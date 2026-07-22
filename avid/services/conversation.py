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
* **State** — it drives the injected ``StateManager`` by **direct call** for exactly three
  edges (SDS §3.10.3): ``CONVERSATION_USER_TRANSCRIBED`` (LISTENING→THINKING),
  ``CONVERSATION_SESSION_LOST`` (any→DEGRADED) and ``SYSTEM_DEGRADED_EXITED`` (DEGRADED→IDLE).
  The LISTENING entry and the THINKING→SPEAKING→IDLE playback arc are **AudioService's**
  (``audio.speech_started`` / ``audio.playback_*``), not this service's.

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

**Stateless instruction in M5.** ``open()`` is where the personality + (later) memory context
is seeded; M5 seeds instructions only. The memory-injection seam is deliberately left empty —
that is M7.
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
from avid.core.ports import Clock, EventBus, RealtimeClient, TurnSink
from avid.core.realtime import (
    AssistantAudioChunk,
    AssistantTranscript,
    SessionClosed,
    TurnDone,
    UserTranscript,
)
from avid.core.state_manager import StateManager
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
    SystemDegradedEntered,
    SystemDegradedExited,
    Trigger,
)
from avid.services.cue_bank import CueBank

_log = logging.getLogger(__name__)

# The component name stamped on the events this service publishes (SDS §9.1.3).
_SOURCE = "ConversationService"

_NS_PER_MS = 1_000_000
_NS_PER_S = 1_000_000_000


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
        session_idle_close_s: int,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._state = state
        self._client = client
        self._sink = sink
        self._cues = cues
        self._idle_close_s = session_idle_close_s

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
            self._turn_id = event.correlation_id
            if not self._session_open:
                await self._client.open()  # cold session; instructions seeded here (M5)
                self._session_open = True
                self._pump_task = asyncio.create_task(
                    self._pump(), name="ConversationService.pump"
                )
                self._mic_task = asyncio.create_task(
                    self._forward_mic(), name="ConversationService.mic"
                )
                if self._degraded:
                    await self._exit_degraded()
            self._arm_idle()

    async def _on_speech_ended(self, event: AudioSpeechEnded) -> None:
        """Re-arm the idle-close timer if a session is live (AC-2).

        Order-tolerant by design: the bus is FIFO *per subscriber*, not across (#72), so this
        can arrive before its ``audio.speech_started`` — in which case there is no session yet
        and there is simply nothing to do. The transcript itself arrives on the event stream,
        never from here.
        """
        async with self._lock:
            if self._session_open:
                self._arm_idle()

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
        resumes for ~200 ms (§6.2.4 trap 1). The truncate/cancel are quick ``RealtimeClient``
        calls; a stray fact with no open session is harmless (the replay records it; a barge-in
        only ever fires with a session live)."""
        if not event.truncated:
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
                case TurnDone():
                    await self._on_turn_done(ev)
                case SessionClosed():
                    await self._on_session_closed(ev)
                case _:  # pragma: no cover - the union is closed; mypy proves this dead
                    assert_never(ev)

    async def _on_user_transcript(self, ev: UserTranscript) -> None:
        """A user utterance was transcribed: a turn begins (SDS §9.1.3).

        Opens the turn (``conversation.turn_started``), publishes
        ``conversation.user_transcribed``, drives LISTENING→THINKING, and kicks a best-effort
        thinking cue to cover the ~600 ms until first audio (SDS §6.9). ``is_approximate`` is
        propagated straight through — a barge-in truncation makes the tail unreliable (§6.2.4).
        """
        self._turn_active = True
        self._turn_started_ns = self._clock.monotonic_ns()
        self._first_audio = False
        await self._publish(ConversationTurnStarted(**self._env(), initiator="user"))
        await self._publish(
            ConversationUserTranscribed(
                **self._env(), text=ev.text, is_approximate=ev.is_approximate
            )
        )
        await self._state.transition(
            Trigger.CONVERSATION_USER_TRANSCRIBED, correlation_id=self._corr()
        )
        self._start_thinking_cue()

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
        await self._sink.play(ev.chunk, item_id=ev.item_id)

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
        was_mid_turn = self._turn_active
        await self._publish(
            ConversationSessionLost(
                **self._env(), cause=ev.cause, was_mid_turn=was_mid_turn
            )
        )
        await self._publish(SystemDegradedEntered(**self._env(), cause=ev.cause))
        await self._state.transition(
            Trigger.CONVERSATION_SESSION_LOST, correlation_id=self._corr()
        )
        self._play_cue(Cue.LOST_CONNECTION)
        self._degraded = True
        self._lost_at_ns = self._clock.monotonic_ns()
        self._turn_active = False
        # We are running *inside* the pump task, so its own cancel is a no-op (see
        # _cancel_task); aclose ends the stream and this loop returns on its own.
        async with self._lock:
            await self._teardown_locked()

    async def _exit_degraded(self) -> None:
        """Recovery (UC-06): a fresh session opened while degraded, so announce it.

        Called from :meth:`_on_speech_started` under the lock, after ``open()`` succeeds. Drives
        DEGRADED→IDLE and publishes ``system.degraded_exited`` with the monotonic downtime.
        ``replay`` has no reconnect/backoff loop (that is #105) — reopen-on-next-speech is the
        honest recovery it affords.
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
        self._idle_task = asyncio.create_task(
            self._idle_timer(), name="ConversationService.idle"
        )

    async def _idle_timer(self) -> None:
        """Close the session after ``session_idle_close_s`` of quiet (AC-3).

        Sleeps on the injected clock (fakeable), so tests advance virtual time instead of
        waiting. On expiry it tears the session down; the timer cancels itself as part of that,
        which :meth:`_cancel_task` makes a no-op (a task never cancels itself)."""
        await self._clock.sleep(self._idle_close_s)
        _log.info("closing idle Realtime session after %ss", self._idle_close_s)
        async with self._lock:
            await self._teardown_locked()

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
        await self._client.aclose()

    def _start_thinking_cue(self) -> None:
        """Kick the best-effort thinking cue for this turn (cancelled when first audio lands)."""
        self._cancel_task(self._thinking_task)
        self._thinking_task = self._play_cue(Cue.THINKING_ONE_SEC)

    def _play_cue(self, cue: Cue) -> asyncio.Task[None]:
        """Play *cue* through the CueBank as a tracked background task (best-effort, SDS §6.9).

        A cue is perceived-quality filler, never a correctness obligation, so it runs off the
        hot path and self-removes from the tracking set on completion; :meth:`stop` sweeps any
        still pending."""
        task = asyncio.create_task(
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
