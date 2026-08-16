"""The conversation loop — one Realtime session into ``conversation.*`` facts (#102, SDS §6.2).

``ConversationService`` is **the heart of M5**: the one service that owns a Realtime session
and translates its neutral :class:`~avid.core.realtime.RealtimeEvent` stream into the five
``conversation.*`` domain facts (``domain/conversation.py``, #99) and the two
``system.degraded_*`` facts (``domain/events.py``, #102). It depends only on the
:class:`~avid.core.ports.RealtimeClient`, :class:`~avid.core.ports.TurnSink`,
:class:`~avid.core.ports.EventBus` and :class:`~avid.core.ports.Clock` **Protocols** — never a
concrete adapter (P2) — plus the shared :class:`~avid.core.state_manager.StateManager`
(SDS §3.8.4) and a :class:`~avid.services.cue_bank.CueBank` it *calls* (SDS §6.9). Because it
names only ports, the identical service runs against the ``replay`` fake on a laptop (#101,
zero network) and the ``openai`` client on the Pi (#105) from one config literal.

It owns three seams, none of which it imports the other side of:

* **The Realtime session** — behind :class:`~avid.core.ports.RealtimeClient`. Ensured on the
  first speech of a turn (``open()``) and closed after ``[gate] session_idle_close_s`` of quiet
  (SDS §6.2/§6.3). There is **no resumption** (SDS §6.2.3): every re-open is a cold session.
* **Audio** — behind :class:`~avid.core.ports.TurnSink` (the ``ConvSvc ↔ AudioSvc`` seam, #103).
  Assistant PCM goes down via :meth:`~avid.core.ports.TurnSink.play` and mic PCM comes up via
  :meth:`~avid.core.ports.TurnSink.mic` — a **direct call, never a bus event** (§9.1.4), because
  audio does not belong on an at-most-once bus.
* **State** — it drives the injected ``StateManager`` by **direct call** for exactly two
  edges (SDS §3.10.3), both of them *session lifecycle*: ``CONVERSATION_SESSION_LOST``
  (any→DEGRADED) and ``SYSTEM_DEGRADED_EXITED`` (DEGRADED→LISTENING). The **whole turn arc** —
  LISTENING, THINKING, SPEAKING, IDLE — is **AudioService's**, driven from its own
  ``audio.*`` facts. LISTENING→THINKING was this service's until AVID-158 measured the
  transcript that drove it arriving *after* the assistant's audio.

**Barge-in is split across the two services (#104, SDS §6.2.4).** AudioService owns the *local*
half — local VAD cuts the speaker instantly (``interrupt()``), measures what actually played,
and moves ``SPEAKING → LISTENING`` — publishing ``audio.playback_finished(truncated=True)`` with
that ``played_ms``. This service owns the *model* half: it subscribes to that fact (SDS §9.1.3
lists it as a subscriber) and, on ``truncated=True``, tells the API the user cut the reply off —
``RealtimeClient.truncate(item_id, played_ms)`` then ``cancel()`` — and **mutes** the assistant
audio deltas still in flight for that item until the next item begins (step 6, not optional: an
un-muted cancelled sentence resumes for ~200 ms). ``played_ms`` crossing on the fact is why the
honest ``audio_end_ms`` — measured at the one layer that owns the speaker — is what the model is
told, so it never believes it said what the user never heard.

**Two origins, one wired.** A turn begins at ``audio.speech_started`` (user) or
``behavior.trigger_fired`` (proactive) — the two events that mint a ``correlation_id`` (SDS
§9.1.1). Only the first exists as an ``Event`` at M5; ``behavior.trigger_fired`` and its
``initiator="proactive"`` path are an M6 seam, so of the two origins :meth:`subscriptions`
wires only ``audio.speech_started`` — exactly as ``AudioService.subscriptions`` returned ``()``
for the ``conversation.*`` it could not yet name. The ``correlation_id`` is **propagated, never
re-minted** (SDS §3.12.2): every fact and every transition carries the id AudioService minted at
``audio.speech_started``, so one grep reconstructs the turn.

**Instruction seeding.** ``open()`` is where the session's static instructions are seeded (the
cached prefix, §6.2.2). Pre-session memory *injection* (§6.7 path 1, #126) composes the top-facts
block and injects it as **layer 4** (§6.4): :meth:`_compose_memory_block` renders it and hands the
awaitable to :meth:`~avid.core.ports.RealtimeClient.open`, which resolves it **concurrently with the
connect** so it costs no wall-clock time. Empty or failed retrieval degrades to the stateless M5
instruction (AC-4/AC-6); every reconnect re-seeds it (cold session, AC-5).

**Memory tools (#125, §6.6, ADR-004).** The model does not own memory; it *gets tools*. When it
invokes one, a :class:`~avid.core.realtime.ToolCallRequested` reaches :meth:`_on_tool_call`, which
dispatches it against the injected :class:`~avid.core.ports.MemoryTools` port
(``remember_fact``/``recall``/``forget``) via :func:`~avid.services.tools.dispatch_tool_call` and
returns the result through :meth:`~avid.core.ports.RealtimeClient.send_tool_output`. The service
depends only on the port, never the concrete ``MemoryService`` (P2/P5) — the composition root
injects it. This is the read/write path (§6.7 path 2); the pre-injection path 1 is #126.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import assert_never, cast
from uuid import UUID

from avid.core.envelope import Envelope, envelope
from avid.core.event_bus import (
    DEFAULT_MAXSIZE,
    Handler,
    OverflowPolicy,
    Subscription,
)
from avid.core.ports import Clock, EventBus, MemoryTools, RealtimeClient, TurnSink
from avid.core.realtime import (
    AssistantAudioChunk,
    AssistantTranscript,
    SessionClosed,
    ToolCallRequested,
    TurnDone,
    UserTranscript,
)
from avid.core.state_manager import StateManager
from avid.core.tasks import spawn
from avid.domain import (
    AudioPlaybackFinished,
    AudioSpeechEnded,
    AudioSpeechStarted,
    ConversationAssistantResponded,
    ConversationSessionLost,
    ConversationTurnEnded,
    ConversationTurnStarted,
    ConversationUserTranscribed,
    Cue,
    Event,
    Fact,
    RobotState,
    SystemDegradedEntered,
    SystemDegradedExited,
    Trigger,
)
from avid.services.cue_bank import CueBank
from avid.services.tools import dispatch_tool_call

_log = logging.getLogger(__name__)

# The component name stamped on the events this service publishes (SDS §9.1.3).
_SOURCE = "ConversationService"

_NS_PER_MS = 1_000_000
_NS_PER_S = 1_000_000_000

# The `cause` stamped on system.degraded_entered when the §6.9 first-token deadline expires
# (AVID-171). Distinct from a socket drop's cause so a bench log — and #106's AC-6 counter —
# can tell "the model went quiet" from "the cable came out".
_THINK_TIMEOUT_CAUSE = "think_timeout"

# The §6.7-path-1 memory block header (§6.4 layer 4). Kept short — the block is billed as input on
# every turn (§6.10), and it is the *only* memory content OpenAI ever sees (§7.10), so it stays lean.
_MEMORY_HEADER = "What you already know about the user (from earlier conversations):"

# Which cue covers a slow first token (AVID-170 AC-4, a decision §6.9 asked to be made rather
# than inherited). §6.9 names a short **"hmm"**; the code shipped `THINKING_ONE_SEC` ("one sec.").
# The spec wins, and not only because it is the spec: "hmm" is a hesitation marker that costs
# nothing if the reply lands immediately after it, while "one sec." *promises* a wait and is
# therefore wrong precisely when the model turns out to be fast. Now that the cue only plays past
# the 600 ms threshold the promise is more defensible than it was — but a filler the user cannot
# be annoyed by is worth more than one that is marginally more apt, which is G3's whole argument.
_THINKING_CUE = Cue.THINKING_HMM


def _format_memory_block(facts: Sequence[Fact]) -> str:
    """Compose the layer-4 injection text from the pre-selected top facts (§6.7 path 1, #126).

    Pure: a short header plus one bullet per fact, in the caller's (recency) order. Returns ``""`` for
    an empty set, so an empty memory injects nothing and the instruction stays the stateless prefix
    (AC-4). Bounding is the retriever's job (``top_facts`` already caps count + tokens, §6.7), so this
    only renders — it never trims."""
    if not facts:
        return ""
    return "\n".join([_MEMORY_HEADER, *(f"- {fact.text}" for fact in facts)])


class ConversationService:
    """Own a Realtime session; publish ``conversation.*`` / ``system.degraded_*`` (SDS §6.2).

    Satisfies the :class:`~avid.core.ports.Service` shape (``name``/``start``/``stop``/
    ``subscriptions``). Like ``AudioService`` it manages owned tasks — a per-session events
    pump, a mic-forward loop, an idle-close timer and best-effort cue tasks — so
    ``lifecycle.run`` ``start``/``stop``\\ s it. Constructed once in the composition root (#102);
    everything it touches is a port, the injected ``StateManager``, or the ``CueBank`` it calls
    (P2, P3). Session-lifecycle mutations are serialised by an ``asyncio.Lock`` so a fresh
    ``audio.speech_started`` and an in-flight teardown cannot interleave.
    """

    name = _SOURCE

    def __init__(
        self,
        *,
        bus: EventBus,
        clock: Clock,
        state: StateManager,
        client: RealtimeClient,
        sink: TurnSink,
        cues: CueBank,
        memory: MemoryTools,
        session_idle_close_s: int,
        memory_inject_timeout_s: float,
        think_timeout_s: float,
        server_turn_detection: bool,
        thinking_delay_ms: int,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._state = state
        self._client = client
        self._sink = sink
        self._cues = cues
        self._memory = memory
        self._idle_close_s = session_idle_close_s
        self._memory_inject_timeout_s = memory_inject_timeout_s
        self._think_timeout_s = think_timeout_s
        # Whether the SERVER is also deciding when a turn ends (AVID-194). Required, never
        # defaulted — the #180 lesson: a defaulted turn-taking knob is one the bench silently
        # never passes, and this one changes who owns the conversation. False is the shipped
        # value and means we are the only authority.
        self._server_turn_detection = server_turn_detection
        # The §6.9 threshold before the thinking cue fills the silence (AVID-170). Required,
        # never defaulted, for the #180 reason: a defaulted perceived-quality knob is one the
        # bench silently never passes, and this one governs how the robot *sounds*.
        self._thinking_delay_ms = thinking_delay_ms

        # Session lifecycle. The lock guards every open/teardown/degraded mutation so the
        # reactive handlers and the owned tasks cannot race the session in or out.
        self._lock = asyncio.Lock()
        self._session_open = False
        # The turn's correlation_id — AudioService minted it at audio.speech_started; every
        # fact and transition here propagates it, never re-mints (SDS §3.12.2).
        self._turn_id: UUID | None = None
        self._turn_active = False  # between conversation.turn_started and turn_ended
        self._turn_started_ns: int | None = None  # monotonic, for turn_ended duration
        self._first_audio = False  # has this turn's first assistant delta arrived yet?
        # Whether this turn's user transcript is approximate (a barge-in truncated the tail,
        # §6.2.4). A remember_fact on such a turn is declined — a half-heard sentence stored as
        # fact is exactly the confabulation §7.6 guards against (#125).
        self._turn_approximate = False
        # Barge-in (§6.2.4 step 6): the response item whose in-flight audio deltas must be
        # dropped after a truncation, until the next assistant item begins. None = not muting.
        self._muted_item: str | None = None
        # Degraded mode: set when the session drops, cleared when a fresh one opens (UC-06).
        self._degraded = False
        self._lost_at_ns: int | None = None  # monotonic, for degraded_exited downtime

        # Owned tasks (SDS §9.2): None until a session opens, cleared on teardown.
        self._pump_task: asyncio.Task[None] | None = None
        self._mic_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        # The thinking cue, cancelled the moment the turn's first assistant delta arrives.
        self._thinking_task: asyncio.Task[None] | None = None
        # The §6.9 first-token deadline (AVID-171), armed at the same falling edge as the cue
        # above and cancelled by the same first delta. The cue is perceived quality; this one is
        # a state edge, and the difference is why it may not ride the bus best-effort.
        self._think_task: asyncio.Task[None] | None = None
        # Best-effort cue tasks, swept on stop().
        self._cue_tasks: set[asyncio.Task[None]] = set()

    # --- SDS §9.2 service shape ----------------------------------------------------------

    async def start(self) -> None:
        """No owned task at rest: the session is ensure-on-speech, so there is nothing to
        launch until an ``audio.speech_started`` arrives. Present for the ``Service`` shape."""

    async def stop(self) -> None:
        """Tear down any live session within the §9.2 5 s budget. Idempotent.

        Cancels the pump/mic/idle/cue tasks and closes the client, so a shutdown mid-turn
        releases the socket rather than leaking it. Runs under the lock so it cannot race a
        concurrent ensure-session.
        """
        async with self._lock:
            await self._teardown_locked()
            self._cancel_cues()

    def subscriptions(self) -> Sequence[Subscription]:
        """Declare the two turn origins plus the barge-in feed (SDS §9.2, §9.1.3).

        ``audio.speech_started`` ensures the session and opens a turn; ``audio.speech_ended``
        re-arms the idle-close timer; ``audio.playback_finished`` is the barge-in feed (#104) —
        on ``truncated=True`` this service runs the §6.2.4 model half (truncate/cancel/mute),
        which is why the §9.1.3 catalog lists it as a subscriber of that fact. The third
        catalogued *origin*, ``behavior.trigger_fired``, has no ``Event`` type yet
        (BehaviorService is M6), so it is a declared seam, not a subscription — the same reason
        ``AudioService.subscriptions`` returned ``()`` at M4. DROP_OLDEST: only the latest edge
        is worth acting on, and the §9.1.5 drift check sees each by its mandatory name.
        """
        return (
            Subscription(
                event_type=AudioSpeechStarted,
                handler=cast(Handler, self._on_speech_started),
                name="ConversationService.speech_started",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=AudioSpeechEnded,
                handler=cast(Handler, self._on_speech_ended),
                name="ConversationService.speech_ended",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=AudioPlaybackFinished,
                handler=cast(Handler, self._on_playback_finished),
                name="ConversationService.playback_finished",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
        )

    # --- turn origins (bus handlers) -----------------------------------------------------

    async def _on_speech_started(self, event: AudioSpeechStarted) -> None:
        """Ensure a session and (re)arm the idle timer (AC-3). The turn origin.

        Adopts the event's ``correlation_id`` (AudioService minted it) as the turn id. If no
        session is open, opens a **cold** one (SDS §6.2.3); if the robot was degraded, that
        successful open *is* the recovery — drive ``SYSTEM_DEGRADED_EXITED`` and announce
        ``system.degraded_exited`` before anything else. The state move to LISTENING is
        AudioService's, already done by the time this runs, so this handler never drives it.
        """
        async with self._lock:
            # The user talking again ends the previous turn's wait, whatever came of it —
            # `(THINKING, AUDIO_SPEECH_STARTED) -> LISTENING` moves the machine out from under an
            # armed deadline, and LISTENING has no THINK_TIMEOUT row (AVID-171). Before the
            # session branch on purpose: it is true whether or not a session is open, and
            # `_cancel_task(None)` is a no-op.
            self._cancel_task(self._think_task)
            self._think_task = None
            self._turn_id = event.correlation_id
            if not self._session_open:
                # Cold session (§6.2.3). The §6.7-path-1 memory block is composed and injected here,
                # overlapping the connect (#126); the client gathers the two. Empty memory / a failed
                # fetch degrades to the stateless M5 instruction (AC-4/AC-6).
                await self._client.open(memory=self._compose_memory_block())
                self._session_open = True
                self._pump_task = spawn(self._pump(), name="ConversationService.pump")
                self._mic_task = spawn(
                    self._forward_mic(), name="ConversationService.mic"
                )
                if self._degraded:
                    await self._exit_degraded()
            self._arm_idle()

    async def _on_speech_ended(self, event: AudioSpeechEnded) -> None:
        """Re-arm the idle-close timer (AC-2) and start the §6.9 thinking cue.

        Order-tolerant by design: the bus is FIFO *per subscriber*, not across (#72), so this
        can arrive before its ``audio.speech_started`` — in which case there is no session yet
        and there is simply nothing to do. The transcript itself arrives on the event stream,
        never from here.

        The cue is armed **here**, at the falling edge, because that is the moment the wait
        actually begins — §6.9's whole job is to cover the gap to first audio. It used to be
        armed when the transcript landed, which the bench showed is 1.1 s *into* the assistant
        already speaking, and once after ``conversation.turn_ended``: since ``CueBank`` plays
        straight to the ``Speaker`` rather than through the ``TurnSink``, the filler was talking
        over the reply it was supposed to cover (AVID-158). It stays best-effort — a cue is
        perceived quality, never a correctness obligation, which is exactly why it may ride the
        bus while the state transition beside it may not.
        """
        async with self._lock:
            if not self._session_open:
                return
            self._arm_idle()
            # `_first_audio` is the turn's own latch: set by the first assistant delta, and
            # cleared HERE — at *every* falling edge, before any early return — because this is
            # the instant one turn's wait for a first token ends and the next one's begins.
            #
            # ⚠️ The reset used to live in `_start_thinking_cue`, which the `already_replying`
            # branch below returns *before* reaching. So the first time a reply beat a falling
            # edge the latch stuck True for the rest of the session, and since AVID-176 gave the
            # local hold a 400 ms margin that is the NORMAL case on turn one. Every later turn
            # then read `already_replying` as True and armed neither the cue nor the §6.9
            # deadline. Measured on the #106 seal run: the robot waited 41 s through a network
            # outage in silence with the deadline set to 10 s, and AC-6 failed. Read the latch
            # and reset it in the same breath; a latch cleared on only one of two paths is not a
            # latch.
            already_replying = self._first_audio
            self._first_audio = False
            if not already_replying:
                # The §6.9 deadline starts at the same instant as the cue below, and for the
                # same reason: this is the moment the wait for a first token actually begins
                # (AVID-171).
                self._arm_think_timeout()
        # The AVID-194 commit, and the reason this service is now the only turn-taking authority.
        # Outside the lock: `end_user_turn` goes to the socket, and the lock guards this service's
        # own state, not the wire. Before the `already_replying` return below, because a turn must
        # be committed whether or not a reply happened to arrive early — an uncommitted buffer is
        # a turn the model never hears, which with the server VAD off nothing else will rescue.
        #
        # Skipped when the server VAD is still configured on: it commits on its own clock, and
        # ours would be a second commit racing it. That configuration stays *reachable* for
        # comparison rather than deleted — but it is not the shipped one, and it is the defect.
        if not self._server_turn_detection:
            await self._client.end_user_turn()
        if already_replying:
            # The reply beat our falling edge. Routine while the server VAD was the other
            # authority (AVID-176's 400 ms margin meant it committed while we were still
            # streaming); with AVID-194 it should be rare, because nothing creates a response
            # before we ask for one. It stays handled: a late overlap from a previous turn can
            # still land here, and arming the §6.9 cue would play "one sec" *over* a reply already
            # coming out of the speaker — the AVID-158 defect (``CueBank`` plays straight to the
            # ``Speaker``, not through the ``TurnSink``), which also breaks ``AlsaSpeaker``'s
            # one-play-in-flight invariant. The deadline is skipped because the first token has
            # already arrived.
            return
        self._start_thinking_cue()

    async def _on_playback_finished(self, event: AudioPlaybackFinished) -> None:
        """The barge-in feed (#104, SDS §6.2.4 steps 4–6): tell the model the user cut it off.

        A *normal* end (``truncated=False``) is nothing of ours — AudioService already published
        it and drove ``SPEAKING → IDLE`` (we return at once). A ``truncated=True`` fact is a
        barge-in: AudioService has already stopped the speaker and measured ``played_ms`` (what
        the speaker *actually* emitted), so we send that honest ``audio_end_ms`` to the model —
        ``truncate`` then ``cancel`` — or it believes it said what the user never heard, which
        poisons the context (§6.2.4 trap 2).

        ``_muted_item`` is set **synchronously, before the awaits**: it is step 6, and the pump
        (a separate coroutine, advancing only at await points) must see it set before it can
        process any post-truncation delta for this item — otherwise the cancelled sentence
        resumes for ~200 ms (§6.2.4 trap 1).

        A truncated fact with **no session open** is dropped. Since AVID-158 the interrupt is
        gated on whether the speaker is live rather than on ``RobotState``, so a response that a
        lost session left un-finalized is now truncated on the next rising edge — and there is
        no longer a client to tell. There is nothing to truncate on a session the model has
        already forgotten, and a fresh cold session must not inherit the mute either."""
        if not event.truncated or not self._session_open:
            return
        self._muted_item = event.item_id  # step 6 — arm the drop before any await
        await self._client.truncate(event.item_id, event.played_ms)  # step 4
        await self._client.cancel()  # step 5

    # --- the events pump -----------------------------------------------------------------

    async def _pump(self) -> None:
        """Consume ``client.events()`` and mint the correlated facts (AC-4).

        Runs until the stream is exhausted (a finite replay) or ``aclose`` halts it. Each
        member is dispatched exhaustively; a raising publish is the bus's problem, never this
        loop's (the bus swallows and republishes ``system.handler_failed``)."""
        async for ev in self._client.events():
            match ev:
                case UserTranscript():
                    await self._on_user_transcript(ev)
                case AssistantTranscript():
                    await self._on_assistant_transcript(ev)
                case AssistantAudioChunk():
                    await self._on_assistant_audio(ev)
                case ToolCallRequested():
                    await self._on_tool_call(ev)
                case TurnDone():
                    await self._on_turn_done(ev)
                case SessionClosed():
                    await self._on_session_closed(ev)
                case _:  # pragma: no cover - the union is closed; mypy proves this dead
                    assert_never(ev)

    async def _on_user_transcript(self, ev: UserTranscript) -> None:
        """A user utterance was transcribed: a turn begins (SDS §9.1.3).

        Opens the turn (``conversation.turn_started``) and publishes
        ``conversation.user_transcribed``. ``is_approximate`` is propagated straight through —
        a barge-in truncation makes the tail unreliable (§6.2.4).

        **Publishes facts; drives nothing** (AVID-158). This used to drive LISTENING→THINKING,
        but the transcript is a separate, slower transcription pass: on hardware it lands after
        the assistant's speech-to-speech audio, and sometimes after ``conversation.turn_ended``.
        The machine follows ``audio.speech_ended`` instead — AudioService's own falling edge,
        which is local, always fires, and is what §3.10.1 has always called "turn end detected".
        """
        self._turn_active = True
        self._turn_started_ns = self._clock.monotonic_ns()
        self._turn_approximate = (
            ev.is_approximate
        )  # gates remember_fact this turn (#125)
        await self._publish(ConversationTurnStarted(**self._env(), initiator="user"))
        await self._publish(
            ConversationUserTranscribed(
                **self._env(), text=ev.text, is_approximate=ev.is_approximate
            )
        )

    async def _on_assistant_transcript(self, ev: AssistantTranscript) -> None:
        """The assistant reply's transcript (``conversation.assistant_responded``). Text only —
        the spoken audio arrives separately as :class:`AssistantAudioChunk` (§9.1.4)."""
        await self._publish(
            ConversationAssistantResponded(
                **self._env(), text=ev.text, item_id=ev.item_id
            )
        )

    async def _on_assistant_audio(self, ev: AssistantAudioChunk) -> None:
        """Push one assistant PCM delta down the ``TurnSink`` (§9.1.4, AC-6).

        Barge-in step 6 (§6.2.4): a delta for the truncated ``_muted_item`` is **dropped here**,
        before it can reach :meth:`TurnSink.play` — that is what silences the cancelled sentence
        already in flight. A delta for any *other* item ends the mute window (the next assistant
        item has begun). The first delta of a turn ends the thinking-cue wait. The play itself is
        a direct awaited call, never a bus event — audio does not belong on an at-most-once bus."""
        if ev.item_id == self._muted_item:
            return  # post-truncation delta of the cancelled item — drop it (§6.2.4 step 6)
        self._muted_item = None  # a delta for a different item ends the mute window
        if not self._first_audio:
            self._first_audio = True
            self._cancel_task(self._thinking_task)
            self._thinking_task = None
            # The token arrived: the §6.9 deadline this turn was racing is void (AVID-171). Here
            # rather than at the playback edge because this is where "first token" is defined,
            # and it runs before `sink.play`, so it always precedes THINKING -> SPEAKING.
            self._cancel_task(self._think_task)
            self._think_task = None
        await self._sink.play(ev.chunk, item_id=ev.item_id)

    async def _on_tool_call(self, ev: ToolCallRequested) -> None:
        """The model requested a tool (§6.6, ADR-004) — execute it against memory and return (#125).

        The dispatch half of "the model gets tools": :func:`~avid.services.tools.dispatch_tool_call`
        parses the call, runs it against the injected :class:`~avid.core.ports.MemoryTools` port
        (``remember_fact``/``recall``/``forget``, never the concrete service — P2/P5), and produces
        the model's tool output; :meth:`RealtimeClient.send_tool_output` returns it **and** sends the
        mandatory ``response.create`` (§6.6's step-5 trap is the adapter's job, not ours), so the
        model speaks its reply. A bad call — unknown tool, malformed arguments, a raising handler — is
        turned into a tool *error* output by the dispatcher and the turn continues; nothing here
        raises into the pump (AC-6). ``_turn_approximate`` gates ``remember_fact`` against the
        §6.2.4/§7.6 barge-in trap. This publishes no ``conversation.*`` fact: the memory write's
        notification is ``memory.fact_stored``, published by ``MemoryService`` itself after the
        durable write (§9.1.4)."""
        output = await dispatch_tool_call(
            self._memory,
            ev,
            correlation_id=self._corr(),
            approximate=self._turn_approximate,
        )
        await self._client.send_tool_output(ev.call_id, output)

    async def _on_turn_done(self, ev: TurnDone) -> None:
        """The turn completed (``conversation.turn_ended``) — the sole cost-meter feed (AC-7).

        Carries the real :class:`~avid.domain.TokenUsage` from ``response.done`` and the turn's
        wall length, measured monotonically (never ``timestamp_ms`` — SDS §9.1.1). Re-arms the
        idle timer: a completed turn is the start of the quiet window before session close."""
        # Finalize the assistant's audio first: the sink publishes audio.playback_finished and
        # drives SPEAKING→IDLE (§9.1.4, a direct call — never a conversation.* subscription).
        await self._sink.end_response()
        duration_ms = 0
        if self._turn_started_ns is not None:
            duration_ms = (
                self._clock.monotonic_ns() - self._turn_started_ns
            ) // _NS_PER_MS
        await self._publish(
            ConversationTurnEnded(
                **self._env(), duration_ms=duration_ms, usage=ev.usage
            )
        )
        self._turn_active = False
        # The (possibly cancelled) response is done — end any barge-in mute (#104).
        self._muted_item = None
        async with self._lock:
            if self._session_open:
                self._arm_idle()

    async def _on_session_closed(self, ev: SessionClosed) -> None:
        """The session dropped mid-stream (UC-06): degrade (AC-4/AC-5/AC-6).

        Publishes ``conversation.session_lost`` (with ``was_mid_turn`` derived from *our* state
        — the client only knows the socket closed) then ``system.degraded_entered``, drives
        any→DEGRADED, and plays a canned CueBank phrase so the robot says *something* with no
        network. Tears the session down; the next ``audio.speech_started`` re-opens cold.
        """
        # Read before _degrade clears it — this is the only caller that has an honest value.
        was_mid_turn = self._turn_active
        await self._publish(
            ConversationSessionLost(
                **self._env(), cause=ev.cause, was_mid_turn=was_mid_turn
            )
        )
        await self._degrade(
            cause=ev.cause,
            trigger=Trigger.CONVERSATION_SESSION_LOST,
            cue=Cue.LOST_CONNECTION,
        )

    async def _degrade(self, *, cause: str, trigger: Trigger, cue: Cue) -> None:
        """Enter DEGRADED and tear the session down — the one path in, whatever the cause.

        Announce (``system.degraded_entered``) → drive *trigger* → say something local → mark the
        outage → tear down. Two callers: :meth:`_on_session_closed` when the socket drops, and
        :meth:`_think_timer` when the model produces no first token (§6.9, AVID-171).

        ``conversation.session_lost`` is deliberately **not** published here. It is loss-specific:
        its first line is "the Realtime session dropped", it carries ``was_mid_turn``, which a
        timeout has no honest value for, and ``conversation_pi.py`` counts it to grade #106's AC-6
        — publishing it on a self-inflicted teardown would make a pulled cable and a model that
        went quiet indistinguishable in the one artifact that grades recovery. ``_idle_timer``
        sets the precedent: it also ends in ``aclose()`` and publishes nothing.

        **The teardown is not tidiness, it is load-bearing** (SDS §3.10.3). DEGRADED has a row for
        neither ``audio.playback_*`` trigger, justified by every path here killing the pump first —
        so a path that left it alive would let a late delta drive ``playback_started`` from
        DEGRADED, where there is no row. It is also what makes recovery reachable at all:
        :meth:`_exit_degraded` fires only when :meth:`_on_speech_started` finds ``_session_open``
        False. Callers may run inside the pump task; its own cancel is a no-op (``_cancel_task``),
        and ``aclose`` ends the stream so the loop returns on its own.
        """
        await self._publish(SystemDegradedEntered(**self._env(), cause=cause))
        await self._state.transition(trigger, correlation_id=self._corr())
        self._play_cue(cue)
        self._degraded = True
        self._lost_at_ns = self._clock.monotonic_ns()
        self._turn_active = False
        async with self._lock:
            await self._teardown_locked()

    async def _exit_degraded(self) -> None:
        """Recovery (UC-06): a fresh session opened while degraded, so announce it.

        Called from :meth:`_on_speech_started` under the lock, after ``open()`` succeeds. Drives
        DEGRADED→**LISTENING** and publishes ``system.degraded_exited`` with the monotonic
        downtime. ``replay`` has no reconnect/backoff loop (that is #105) —
        reopen-on-next-speech is the honest recovery it affords.

        LISTENING, not IDLE, since AVID-162. This method's single call site *is* the rising-edge
        handler, so recovery is never anything but mid-turn: the user is talking right now, and
        that is what triggered the reopen. Landing in IDLE left the whole recovery turn driving
        illegal transitions — it never reached THINKING or SPEAKING, so the first turn after the
        robot had been broken had no thinking face, no speaking face, and no state move behind a
        barge-in. If a background reconnect is ever added it will recover with *no* turn in
        flight, and it needs its own trigger rather than this one (SDS §3.10.3).
        """
        downtime_s = 0.0
        if self._lost_at_ns is not None:
            downtime_s = (self._clock.monotonic_ns() - self._lost_at_ns) / _NS_PER_S
        await self._state.transition(
            Trigger.SYSTEM_DEGRADED_EXITED, correlation_id=self._corr()
        )
        await self._publish(SystemDegradedExited(**self._env(), downtime_s=downtime_s))
        self._degraded = False
        self._lost_at_ns = None

    # --- memory injection (§6.7 path 1, #126) --------------------------------------------

    async def _compose_memory_block(self) -> str:
        """Fetch the top facts and render the layer-4 injection block, or ``""`` (§6.7 path 1, AC-4/AC-6).

        Awaited by the client **concurrently with the connect** (the awaitable handed to
        :meth:`~avid.core.ports.RealtimeClient.open`), so the ~30 ms local retrieval overlaps the
        ~150 ms WSS setup and costs no wall-clock time (AC-1). Retrieval is off the turn path, but a
        hung store must not delay time-to-session-ready past budget, so it is bounded by
        ``memory_inject_timeout_s``; a timeout **or** any retrieval failure is logged with the turn's
        correlation id and degrades to an empty block — the robot still talks, it just does not
        remember this session (AC-6). Runs on every open, so a reconnect re-seeds the same memory
        (AC-5)."""
        try:
            facts = await asyncio.wait_for(
                self._memory.top_facts(), self._memory_inject_timeout_s
            )
        except Exception:  # noqa: BLE001 - AC-6: a retrieval failure/timeout must not block the session
            _log.warning(
                "memory injection failed [%s] — opening the session without it",
                self._corr(),
                exc_info=True,
            )
            return ""
        return _format_memory_block(facts)

    # --- mic forwarding ------------------------------------------------------------------

    async def _forward_mic(self) -> None:
        """Forward captured mic PCM up to the model (§9.1.4, AC-6).

        Drains :meth:`TurnSink.mic` and hands each frame to
        :meth:`RealtimeClient.send_audio` — a direct call, never blocking the loop (P8). Ends
        when the mic stream ends (a finite fake turn) or when teardown cancels it."""
        async for chunk in self._sink.mic():
            await self._client.send_audio(chunk)

    # --- idle close ----------------------------------------------------------------------

    def _arm_idle(self) -> None:
        """(Re)start the idle-close countdown from *now*. Caller holds the lock."""
        self._cancel_task(self._idle_task)
        self._idle_task = spawn(self._idle_timer(), name="ConversationService.idle")

    async def _idle_timer(self) -> None:
        """Close the session after ``session_idle_close_s`` of quiet (AC-3).

        Sleeps on the injected clock (fakeable), so tests advance virtual time instead of
        waiting. On expiry it tears the session down; the timer cancels itself as part of that,
        which :meth:`_cancel_task` makes a no-op (a task never cancels itself)."""
        await self._clock.sleep(self._idle_close_s)
        _log.info("closing idle Realtime session after %ss", self._idle_close_s)
        async with self._lock:
            await self._teardown_locked()

    # --- the §6.9 first-token deadline (AVID-171) ----------------------------------------

    def _arm_think_timeout(self) -> None:
        """(Re)start the first-token countdown from *now*. Caller holds the lock."""
        self._cancel_task(self._think_task)
        self._think_task = spawn(
            self._think_timer(), name="ConversationService.think_timeout"
        )

    async def _think_timer(self) -> None:
        """Degrade if the model produces no first token within ``think_timeout_s`` (§6.9).

        Sleeps on the injected clock, exactly like :meth:`_idle_timer`. The three guards below
        are not defensive padding — each covers a *reachable* arc that leaves this timer armed:

        * ``_first_audio`` — the delta landed on the same tick the deadline expired.
        * ``_session_open`` — an idle close or :meth:`stop` got there first.
        * **the state re-check** — AVID-161's overlap, and the only one no cancel site can
          reach. A reply to an *earlier* turn draining while this one waits drives
          ``THINKING + audio.playback_finished -> IDLE``, leaving this timer armed in IDLE with
          no first audio of its own and no rising edge to cancel it. Only
          ``(THINKING, THINK_TIMEOUT)`` exists, so firing there would log an "ignored illegal
          transition" WARNING on ``avid.state`` — the noise the M5 gate forbids, and the exact
          class of bug AVID-158/161/162 were. Cancelling on ``turn_done`` instead would be
          wrong: an earlier turn finishing says nothing about *this* turn's first token.

        Degrading here is a deliberate give-up on a socket that is still open, which is why it
        goes through :meth:`_degrade` (tearing the session down) and publishes no
        ``conversation.session_lost`` — see that method.
        """
        await self._clock.sleep(self._think_timeout_s)
        if self._first_audio or not self._session_open:
            return
        if self._state.state is not RobotState.THINKING:
            _log.debug(
                "think timeout elapsed in %s, not THINKING — not degrading [correlation_id=%s]",
                self._state.state.name,
                self._corr(),
            )
            return
        _log.warning(
            "no first token after %ss — degrading [correlation_id=%s]",
            self._think_timeout_s,
            self._corr(),
        )
        await self._degrade(
            cause=_THINK_TIMEOUT_CAUSE,
            trigger=Trigger.THINK_TIMEOUT,
            cue=Cue.SOMETHING_WRONG,
        )

    # --- teardown & task helpers ---------------------------------------------------------

    async def _teardown_locked(self) -> None:
        """Close the session and cancel its owned tasks. Caller holds the lock; idempotent.

        Cancels the pump/mic/idle tasks (skipping whichever is the current task, so a
        teardown invoked from inside one of them does not try to cancel itself) and closes the
        client. The thinking cue is stopped; other best-effort cue tasks are left to finish
        (they release themselves) but are swept on :meth:`stop`."""
        self._session_open = False
        # A fresh cold session must not inherit a stale barge-in mute (#104).
        self._muted_item = None
        self._cancel_task(self._idle_task)
        self._idle_task = None
        self._cancel_task(self._mic_task)
        self._mic_task = None
        self._cancel_task(self._pump_task)
        self._pump_task = None
        self._cancel_task(self._thinking_task)
        self._thinking_task = None
        self._cancel_task(self._think_task)
        self._think_task = None
        await self._client.aclose()

    def _start_thinking_cue(self) -> None:
        """Arm the §6.9 thinking cue: play it **only** if first audio is late (AVID-170).

        The ``_first_audio`` latch that cancels this cue is re-armed by the caller, at the
        falling edge, on **every** path — not here. It lived here until AVID-186, which is
        precisely why it stopped being re-armed once the caller grew a path that skips the cue.
        """
        self._cancel_task(self._thinking_task)
        self._thinking_task = spawn(
            self._thinking_cue_after_delay(),
            name="ConversationService.thinking_cue",
        )

    async def _thinking_cue_after_delay(self) -> None:
        """Wait ``[cues] thinking_delay_ms``, then fill the silence if nothing has arrived.

        §6.9 specifies a *threshold*, not a delay: *"if first audio hasn't arrived by 600 ms we
        play a short local 'hmm'"*. There was no timer — the cue was scheduled immediately and
        only cancellation stopped it, which is a race the cue reliably won because it starts
        pushing a WAV to ALSA in the same tick the user stops speaking. So it played on **every**
        turn, and a mitigation for occasional slowness became a permanent verbal tic. Reported by
        the owner as *"hearing a lot of one second"* and first suspected to be a network problem.

        It also made fast turns *sound* slower than they were, which is the precise inverse of
        R-01's argument: perceived latency is designable, and this was designing it upward.

        The wait is on the **injected clock**, so tests drive it in virtual time and the deadline
        is exact rather than approximately-600-ms-plus-scheduler. Cancellation is the first
        delta's job (:meth:`_on_assistant_audio` cancels this task), so a reply that lands during
        the wait leaves no partial WAV anywhere near the speaker — the cue has not begun.
        """
        if self._thinking_delay_ms > 0:
            # Zero means "no threshold" — the pre-AVID-170 behaviour, still a legal setting. The
            # branch is not decoration: `FakeClock.sleep` parks until a test advances time, so a
            # zero-second sleep on it would block forever rather than return immediately.
            await self._clock.sleep(self._thinking_delay_ms / 1000)
        if self._first_audio:
            # Belt to the cancellation's braces: if the delta landed in the same tick as the
            # deadline, cancelling and this check race, and a spurious cue is exactly the defect.
            return
        await self._cues.play(_THINKING_CUE, correlation_id=self._corr())

    def _play_cue(self, cue: Cue) -> asyncio.Task[None]:
        """Play *cue* through the CueBank as a tracked background task (best-effort, SDS §6.9).

        A cue is perceived-quality filler, never a correctness obligation, so it runs off the
        hot path and self-removes from the tracking set on completion; :meth:`stop` sweeps any
        still pending."""
        task = spawn(
            self._cues.play(cue, correlation_id=self._corr()),
            name=f"ConversationService.cue.{cue.name}",
        )
        self._cue_tasks.add(task)
        task.add_done_callback(self._cue_tasks.discard)
        return task

    def _cancel_cues(self) -> None:
        """Cancel every pending best-effort cue task (used by :meth:`stop`)."""
        for task in tuple(self._cue_tasks):
            self._cancel_task(task)

    @staticmethod
    def _cancel_task(task: asyncio.Task[None] | None) -> None:
        """Cancel *task* unless it is done or is the currently running task.

        Skipping the current task is what lets teardown be called from inside the pump or the
        idle timer without a coroutine trying to cancel itself."""
        if task is None or task.done():
            return
        with contextlib.suppress(RuntimeError):  # no running loop (defensive)
            if task is asyncio.current_task():
                return
        task.cancel()

    # --- envelope helpers ----------------------------------------------------------------

    def _corr(self) -> UUID:
        """The current turn's correlation_id. Set at the origin; pump code only runs with a
        session open, so it is never ``None`` here."""
        assert self._turn_id is not None  # a session is open ⇒ an origin set this
        return self._turn_id

    def _env(self) -> Envelope:
        """A fresh event envelope stamped with the turn's correlation_id (SDS §9.1.1).

        Returns the :class:`~avid.core.envelope.Envelope` TypedDict so ``**self._env()`` unpacks
        into an ``Event`` constructor with the base fields typed — the same idiom AudioService
        uses inline (``avid/services/audio.py``)."""
        return envelope(clock=self._clock, correlation_id=self._corr(), source=_SOURCE)

    async def _publish(self, event: Event) -> None:
        """Publish one domain fact. Thin wrapper so each mint reads as a single line."""
        await self._bus.publish(event)
