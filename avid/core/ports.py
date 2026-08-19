"""Port contracts — what the application needs from the physical world (AVID-11).

The inner half of the hexagonal boundary (ADR-003). Each ``Protocol`` here is
defined by *what the application needs*, never by what a device offers — that
inversion is the whole value (SDS §3.9.1). Adapters in ``avid.adapters`` satisfy
these structurally; ``main.py`` alone wires which one (P2, P3).

Ports defined here (SDS §3.5.2, §3.9.1, §9.3): :class:`EventBus`, :class:`Clock`,
:class:`Camera`, :class:`Servo`, :class:`Display`, :class:`Microphone`,
:class:`Speaker`, :class:`VoiceActivityDetector`, :class:`FaceDetector`,
:class:`RealtimeClient`, :class:`TurnSink`, :class:`FactRepository`,
:class:`Embedder`, :class:`Retriever`, :class:`TextModel`, :class:`MemoryTools`,
:class:`AffectTools`, :class:`EpisodeStore`.

:class:`Service` is the odd one out: not a device port but the SDS §9.2 shape every
use-case service takes (``name``/``start``/``stop``/``subscriptions``), so
``lifecycle.run`` can own their loops and ``main.py`` can register their subscriptions
without naming a concrete service (P2). It lives here beside the ports because it is the
same kind of thing — a structural contract the application depends on rather than a
class — even though what it abstracts is inward (a service) rather than outward (a
device).

Every port is ``@runtime_checkable`` (AVID-11 acceptance). Note that
``isinstance`` against a runtime-checkable ``Protocol`` verifies member *presence*,
not signatures — the type checker enforces the shapes; the decorator lets the
composition root and tests assert an object is port-shaped at all.

The value types crossing these boundaries live in :mod:`avid.core.hal`. Imports
point only within ``core`` and to ``domain`` (P1): ``ports`` reads
:class:`~avid.core.event_bus.Subscription` from the concrete bus module, which
never imports back — the dependency is one-directional, no cycle.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import UUID

from avid.core.event_bus import E, Subscription
from avid.core.hal import (
    AudioChunk,
    Axis,
    CameraCaps,
    Detection,
    DisplayFrame,
    Frame,
)
from avid.core.realtime import RealtimeEvent
from avid.core.schedule import Routine
from avid.domain import (
    Affect,
    Event,
    Fact,
    RetrievalMatch,
    RoutineSpec,
    TriggerRecord,
)


@runtime_checkable
class EventBus(Protocol):
    """The bus as its publishers and subscribers need it (SDS §3.5.2).

    The concrete :class:`~avid.core.event_bus.AsyncioEventBus` satisfies this
    without change; application code depends on this port, not the class (P2).
    """

    async def publish(self, event: Event) -> None:
        """Fire-and-forget: returns once the event is queued, not once handled.
        Never raises because a subscriber failed (SDS §3.5.2)."""
        ...

    def subscribe(
        self,
        event_type: type[E],
        handler: Callable[[E], Awaitable[None]],
        *,
        name: str,
    ) -> Subscription:
        """Register a handler for one event type. ``name`` is mandatory: it is
        how the §9.1.5 subscriber-graph/drift check sees the subscriber (an
        anonymous lambda would be invisible to it)."""
        ...


@runtime_checkable
class Clock(Protocol):
    """Time, injected so it can be faked (SDS §9.3).

    Two readings, matching the :class:`~avid.domain.Event` envelope's two time
    fields: wall-clock for humans, monotonic for arithmetic. Never subtract wall
    time (SDS §9.1.1). ``FakeClock`` (AVID-12) drives ``sleep`` so time-dependent
    behaviour is a millisecond test, not a morning's wait.
    """

    def now(self) -> int:
        """Epoch **seconds**, wall clock — for logs and persistence (SDS §8.2)."""
        ...

    def monotonic_ns(self) -> int:
        """``time.monotonic_ns()`` — for latency math (SDS §9.1.1)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """The injectable, fakeable replacement for ``asyncio.sleep``."""
        ...


@runtime_checkable
class Camera(Protocol):
    """A source of frames (SDS §3.9.1)."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def capture(self) -> Frame:
        """Latest frame. Never blocks the loop; may return a repeat frame."""
        ...

    @property
    def capabilities(self) -> CameraCaps:
        """What this camera can do, for negotiation (SDS §3.9.3) — so services
        adapt to the rig they were given rather than assuming one."""
        ...


@runtime_checkable
class Servo(Protocol):
    """Actuation, defined so the gesture engine stays axis-agnostic (SDS §3.9.1)."""

    async def move_to(
        self, channel: int, angle_deg: float, *, duration_ms: int
    ) -> None:
        """Move ``channel`` smoothly to ``angle_deg`` over ``duration_ms``.

        Clamping ``angle_deg`` to the configured safe limits is the **adapter's**
        job, not the caller's (SDS §3.9.1): the safety limit is a property of the
        physical linkage, so the invariant belongs at the lowest layer that can
        enforce it universally. The move MUST be cancellable — a preempting
        gesture cancels this task.
        """
        ...

    async def relax(self, channel: int) -> None:
        """De-energize the channel. Prevents servo buzz and heat when idle."""
        ...

    @property
    def axes(self) -> tuple[Axis, ...]:
        """The axes this rig exposes, for negotiation (SDS §3.9.3 / ADR-009)."""
        ...


@runtime_checkable
class Display(Protocol):
    """A surface for the robot's face (SDS §3.9.1)."""

    async def render(self, frame: DisplayFrame) -> None:
        """Show a **frame**, not a screen. The port never promised a
        framebuffer — only pixels — so a real adapter can render offscreen and
        push RGB565 over ``spidev`` while a fake writes a PNG (AVID-11)."""
        ...

    @property
    def resolution(self) -> tuple[int, int]:
        """``(width, height)`` in pixels."""
        ...


@runtime_checkable
class ServiceNotifier(Protocol):
    """The process supervisor, as the lifecycle needs it (AVID-38, SDS §3.11.3).

    ``Type=notify`` supervision inverted into a port: the application announces its
    own liveness rather than the supervisor probing it. Three facts, in the order the
    run loop states them — up, still-alive, going-down — so a wedged loop that stops
    pinging is restarted (the watchdog), not left dead until someone notices.

    The real adapter (:class:`~avid.adapters.notifier.SystemdNotifier`) speaks the
    ``sd_notify`` datagram protocol to ``$NOTIFY_SOCKET``; the fake
    (:class:`~avid.adapters.notifier.FakeServiceNotifier`) records the calls and *is*
    the simulator (P6). Off systemd — the laptop profile — the fake is wired and the
    supervisor simply does not exist, so nothing is lost.

    All three are ``async`` for a uniform port, though the real send is a single
    non-blocking datagram: notifications are best-effort, never a reason to block the
    loop (P8) or to raise into it (SDS §3.12.3 — nothing but a bad key at boot stops
    the robot).
    """

    async def ready(self) -> None:
        """Announce ``READY=1`` — the app has reached IDLE and is serving."""
        ...

    async def watchdog(self) -> None:
        """Send one ``WATCHDOG=1`` keep-alive. The loop pings on an interval shorter
        than the unit's ``WatchdogSec``; a missed ping is how a wedged loop is caught."""
        ...

    async def stopping(self) -> None:
        """Announce ``STOPPING=1`` — a deliberate shutdown, so the restart policy
        distinguishes it from a crash."""
        ...


@runtime_checkable
class Microphone(Protocol):
    """A stream of captured audio (SDS §3.9.1)."""

    def stream(self) -> AsyncIterator[AudioChunk]:
        """Yield audio chunks as they are captured."""
        ...


@runtime_checkable
class Speaker(Protocol):
    """Audio output, including barge-in (SDS §3.9.1)."""

    async def play(self, chunk: AudioChunk) -> int:
        """Play one chunk of synthesized audio; return the milliseconds the device **accepted**.

        A write that moved no samples must be distinguishable from one that moved all of
        them. ALSA returns ``-EPIPE`` after an underrun having played nothing — measured on
        the Pi at the M4 gate as ``write 1: 48000 @1.898s / write 2: -32 @0.000s /
        write 3: 48000 @1.909s`` — so an adapter that discards its own return drops every
        other utterance in silence, and the caller then publishes
        ``audio.playback_finished`` for audio the room never heard (AVID-91). The figure
        therefore crosses the port: ``AudioService`` sums *this*, never the length of the
        buffer it submitted.

        **Accepted, not emitted.** A device acknowledges frames *into its ring buffer*, not
        out of its DAC. So this is exact about **drops** and optimistic by up to one buffer
        depth (~107 ms at 24 kHz) about **photons** — the residual §6.2.4 step 3 already
        carries. A caller needing sub-buffer precision must query the device, which this
        port deliberately does not expose.

        The chunk's ``sample_rate``/``channels`` are **honoured, not advisory** — the fields
        exist so a consumer can obey them. Playing 16 kHz PCM at a configured 24 kHz is
        1.5x fast and a fifth high (measured: 6.00 s of capture echoed in 4.01 s). Only
        sample *width* is implicit (S16_LE), because ``AudioChunk`` carries no field for it.
        """
        ...

    async def play_file(self, path: Path) -> int:
        """Play a WAV from disk — the degraded-mode canned-response bank.

        Returns the milliseconds the device accepted, under the same reading as
        :meth:`play`. A barge-in truncates the clip, so a short return is a **fact**, not an
        error — the caller decides whether a cue that did not finish is worth logging.
        """
        ...

    async def stop(self) -> None:
        """Stop immediately. On the port so barge-in is genuinely instant."""
        ...


@runtime_checkable
class VoiceActivityDetector(Protocol):
    """The local session gate — is this frame speech, or silence/noise (SDS §6.3, §9.3)?

    The one question the AI-cost gate turns on (ADR-007, *accepted*): no Realtime session
    opens until this says speech. Streaming continuously is ~$670/mo; gating on local VAD is
    ~$4/mo (SPK-1, SDS §6.10) — so the port exists to keep a paid session from opening on a
    door slam, which a loudness threshold cannot tell from a word (SDS §6.3).

    The port is defined by *what the application needs* — a per-frame yes/no on an
    :class:`~avid.core.hal.AudioChunk` — never by what the detector offers: no probabilities,
    no model handles, no vendor types cross it. Choosing a probability threshold is the
    **adapter's** business, so swapping Silero for another detector is one adapter, not a
    ripple through the service (P2).
    """

    def is_speech(self, frame: AudioChunk) -> bool:
        """Whether *frame* is speech. **Synchronous and fast** — SDS §9.3 budgets <5 ms,
        called on every frame — so a real detector runs inference in-process rather than
        blocking the loop (the sub-ms Silero cost stays under the 50 ms slow-callback gate,
        P8), and the caller (AudioService) invokes it inline, not via an executor."""
        ...


@runtime_checkable
class FaceDetector(Protocol):
    """What is in **this one frame** — the person-detection port (SDS §3.6.5, ADR-013).

    Named after the sibling it most resembles: :class:`VoiceActivityDetector` is also a
    model behind a port, also frame-by-frame, also ONNX underneath. The same inversion
    applies — the port is defined by what ``PresenceService`` needs, one frame in and this
    frame's faces out, never by what a detection library offers. Landmarks, keypoints,
    tracking ids and identity embeddings all stay on the adapter's side of the boundary,
    which is what makes swapping YuNet for something else one adapter rather than a ripple
    (P2), and what makes the fake honest to write (P6).

    **The port never promises "is a person present."** It reports what it saw in one frame;
    presence is a *decision over time*, and it belongs to the pure hysteresis filter in
    :mod:`avid.domain.vision` (SDS §9.1.3). This is the single most important line in the
    milestone, so it is stated rather than implied: a port that answered "present?" would put
    the debouncing behind a device boundary, where it can be neither unit-tested nor replayed
    against a recorded hour — and M8's headline criterion is *no flapping*, which is exactly
    the property that has to be replayable.

    It also takes no view on **identity**. This is presence, not recognition (ADR-013, §13):
    telling *who* someone is has materially different privacy consequences and is a different
    problem, and nothing on this port could express it.
    """

    async def detect(self, frame: Frame) -> Sequence[Detection]:
        """Every face in *frame*, best-first. Empty when there are none — not an error.

        **Asynchronous**, unlike :meth:`VoiceActivityDetector.is_speech`, and the difference
        is a budget rather than a style: Silero is sub-millisecond so it runs inline, while
        face inference is tens of milliseconds and would breach the 50 ms slow-callback gate
        (P8). So the adapter owns its own offload behind this method, exactly as
        :meth:`Camera.capture` and :meth:`Servo.move_to` do — the caller awaits and is
        promised only that the loop is never blocked.

        The frame's own ``format``/``width``/``height`` are **honoured, not assumed**: an
        adapter that cannot read a frame's format must say so rather than return an empty
        sequence, because a detector that silently detects nothing is indistinguishable from
        an empty room and would leave every downstream test green while the robot never
        notices anyone.
        """
        ...


@runtime_checkable
class RealtimeClient(Protocol):
    """The OpenAI Realtime session, as ``ConversationService`` needs it (SDS §3.9.1, §6.2).

    The vendor boundary as a port (CLAUDE.md §3, R-10). ``ConversationService`` (#102) depends
    only on this; the ``replay`` (#101) and ``openai`` (#105) adapters satisfy it. The port is
    in *our* vocabulary — a session to open/close, PCM to send, a stream of neutral events to
    consume, a barge-in truncate/cancel — never OpenAI's message shapes. What crosses is the
    :class:`~avid.core.realtime.RealtimeEvent` union; the vendor's wire format is the adapter's
    private business, so if the Realtime API changes, exactly one adapter changes and this port,
    the service, and the domain do not (PMP §9.2).

    A session is **cold** — there is no resumption (SDS §6.2.3): a dropped connection means a new
    session, re-seeded with instructions + memory, which is why :meth:`open` /:meth:`aclose` are
    an explicit lifecycle rather than a constructor detail.
    """

    async def open(self, *, memory: Awaitable[str] | None = None) -> None:
        """Open a fresh session (instructions + tools + injected memory) — cold, no resume.

        ``memory`` is an awaitable resolving to the §6.7-path-1 **layer-4 instruction block** — the
        pre-session facts (§6.4), already composed to text and bounded by the caller (#126). The
        adapter awaits it **concurrently with the socket connect** (``asyncio.gather``), not after, so
        the ~30 ms local retrieval overlaps the ~150 ms WSS setup and costs zero wall-clock time
        (AC-1); the single ``session.update`` then carries layers 1–3 (the static cached prefix,
        §6.2.2) **plus** that block appended as layer 4, so the prefix stays byte-identical and cached
        (AC-2). ``None`` (or an empty resolved string) seeds the stateless M5 instruction unchanged
        (AC-4). Every open re-runs it — a reconnect is a cold session re-seeded with memory (§6.2.3,
        AC-5). The caller owns the fetch's error/empty/timeout handling, so what crosses here is only
        ever a ready string."""
        ...

    async def aclose(self) -> None:
        """Tear the session down and release the socket. Idempotent."""
        ...

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Send one captured mic chunk up to the model. Never blocks the loop (P8)."""
        ...

    def events(self) -> AsyncIterator[RealtimeEvent]:
        """Yield neutral, typed session events (SDS §3.9.1). Vendor-free by construction:
        every provider message is mapped to a :class:`~avid.core.realtime.RealtimeEvent`
        before it crosses, so no Realtime shape leaks past the adapter."""
        ...

    async def end_user_turn(self) -> None:
        """The user has stopped speaking — close their turn and ask for a reply (AVID-194).

        Called from the local VAD's falling edge, and it is what makes **us** the single
        turn-taking authority. Until AVID-194 the system ran two independent VADs over the same
        audio, both authoritative: ours at ``[gate] silence_hold_ms`` and OpenAI's at
        ``[ai.turn_detection] silence_duration_ms``. A 500–900 ms pause *is ordinary speech* —
        drawing breath, hesitating before a name — so inside that window the server committed and
        answered a fragment while our gate still considered the utterance open. Whichever window
        is smaller commits first, by construction; there is no assignment of the two knobs that
        leaves only one of them deciding.

        **Named for what the application needs, not for the frames it becomes.** The vendor's
        version of this is ``input_audio_buffer.commit`` followed by ``response.create`` — two
        frames, in that order, with the second mandatory or the model silently sits (§6.6's
        step-5 trap). Which of those an adapter sends, and whether it sends any, is the adapter's
        business (ADR-003): a replay fixture answers on its own recorded schedule and needs to
        send nothing at all.

        Idempotence is the adapter's business too: a falling edge with nothing buffered must not
        raise, because the caller cannot know what the socket has seen.
        """
        ...

    async def begin_proactive_turn(self) -> None:
        """Ask the model to speak when **nobody has spoken** — §10.7 step 3, and UC-03's whole trick.

        Every other session in this system opens because a person made a sound. This one opens
        because a clock did: *"the Realtime API doesn't care that nobody spoke."*

        **Named for what the application needs, not for the frames it becomes** (ADR-003), exactly
        as :meth:`end_user_turn` is. The one thing this port *does* promise about frames is a
        negative, because it is the whole point of the method: **no input audio buffer is committed
        or appended on this path, ever.** The session carries instructions, injected memory and the
        §10.8 context block, and nothing else — there is no user utterance to commit, and
        committing an empty one would be a request for a reply to silence.

        Idempotence and in-flight handling are the adapter's business. The real client already has
        the machinery: it waits for any active response to finish before creating another (the #284
        guard), and that wait fails *open* — on timeout the create goes out anyway, because a
        dropped reply is a turn the user waited on and never got.
        """
        ...

    async def truncate(self, item_id: str, audio_end_ms: int) -> None:
        """Barge-in step 4 (§6.2.4): tell the model the user cut ``item_id`` off at
        ``audio_end_ms`` — **what the speaker actually played** (from
        :meth:`TurnSink.stop`), not what we received, or the model believes it said
        things the user never heard. The Realtime ``content_index`` (always 0 for us) is
        the adapter's detail, kept off this port."""
        ...

    async def cancel(self) -> None:
        """Barge-in step 5 (§6.2.4): cancel the in-flight response so the model stops
        generating the interrupted turn."""
        ...

    async def send_tool_output(self, call_id: str, output: str) -> None:
        """Return a tool's result to the model — the §6.6 return leg (ADR-004).

        The model asks for a tool via a :class:`~avid.core.realtime.ToolCallRequested` on
        :meth:`events`; the dispatcher executes it and hands the result back here, ``output``
        already serialised to a string, ``call_id`` echoed from the request. The adapter then
        **must** prompt the model to speak (``response.create``) or it silently swallows the
        turn — §6.6's number-one "why is it doing nothing" bug (the step-5 trap), so that follow
        is the adapter's responsibility, not the caller's. Tool *declarations* are session-level
        (part of the cached prefix, §6.2.2), so they are configured at construction, not here."""
        ...


@runtime_checkable
class TurnSink(Protocol):
    """The audio seam between ``ConversationService`` and ``AudioService`` (SDS §9.1.4).

    PCM is a **direct call, never a bus event** (§9.1.4): audio does not belong on an
    at-most-once bus. This port is how a turn's audio crosses ``ConvSvc ↔ AudioSvc`` without
    the two services importing each other (P5) — the conversation service reads captured mic
    frames off :meth:`mic` to forward to the model, and pushes assistant PCM down through
    :meth:`play`. Its real implementation lands with the ``AudioService`` seam (#103); the
    :class:`~avid.adapters.turn_sink.FakeTurnSink` is the simulator (P6).

    **Concurrency contract** (AVID-174). The three playback methods are **not** called from one
    task. :meth:`play` and :meth:`end_response` come from ``ConversationService``'s Realtime pump;
    :meth:`interrupt` comes from whichever task detects the barge-in, which for the real sink is
    ``AudioService``'s own mic loop. An implementation must therefore assume:

    * :meth:`interrupt` **may be called while a** :meth:`play` **is suspended mid-write**, and must
      not wait for it — barge-in is the one thing §6.2.4 requires to be immediate.
    * :meth:`end_response` and :meth:`interrupt` are **mutually exclusive finalizers of the same
      playback episode, and exactly one may win.** Finalizing twice publishes a second
      ``audio.playback_finished``, which makes the conversation service truncate a response that
      ended normally.
    * A :meth:`play` whose episode was finalized while it was suspended must **discard** its result
      rather than write it back — those ms belong to a playback that has already ended.
    """

    def mic(self) -> AsyncIterator[AudioChunk]:
        """Yield captured mic PCM (up), mirroring :meth:`Microphone.stream` — the frames the
        conversation service forwards to the model via :meth:`RealtimeClient.send_audio`."""
        ...

    async def adopt_turn(self, correlation_id: UUID) -> None:
        """Tell the sink which turn the audio about to arrive belongs to (§9.1.1, #337).

        ⚠️ **Only the proactive origin needs this, and that asymmetry is the point.** A user turn
        begins with the sink's *own* VAD rising edge, so it mints the id itself and already knows.
        A proactive turn begins with a clock — the sink hears nothing, mints nothing, and would
        otherwise receive assistant audio belonging to a turn it has never heard of.

        Found on the rig: without it ``AudioService`` asserted on a ``None`` turn id the instant the
        first audio chunk arrived, killing the conversation pump. The robot fired its reminder,
        opened a session, and said **nothing**.

        It rides this port rather than the bus deliberately. §9.1.4 makes the ConvSvc↔AudioSvc seam
        a direct call because audio does not belong on an at-most-once bus, and a turn's *identity*
        travels with its audio — putting it back on the bus would reintroduce the very edge
        ``AudioService.subscriptions`` returns ``()`` to avoid.
        """
        ...

    async def play(self, chunk: AudioChunk, *, item_id: str) -> None:
        """Play one assistant PCM delta (down), tagged with the ``item_id`` a barge-in may
        later truncate. A §9.1.4 direct call — never blocks the loop (P8)."""
        ...

    async def end_response(self) -> None:
        """The assistant's audio for the in-flight response is complete — a *normal* end, not a
        barge-in. The sink finalizes playback: it publishes ``audio.playback_finished`` with
        ``truncated=False`` and drives ``SPEAKING → IDLE`` (§3.10.3). A no-op if nothing is
        playing (a turn that produced no audio).

        Distinct from :meth:`interrupt` on purpose: only ``ConversationService`` knows a
        response finished (it receives the model's ``response.done``), and the two ways playback
        ends have **different** state outcomes — normal completion → IDLE, barge-in → LISTENING.
        So the service calls this on ``TurnDone`` and calls :meth:`interrupt` on a barge-in."""
        ...

    async def interrupt(self) -> int:
        """Barge-in: stop playback immediately and return ``played_ms`` — the milliseconds the
        speaker **actually** emitted, not what was received (§6.2.4). That figure is the honest
        ``audio_end_ms`` :meth:`RealtimeClient.truncate` needs; the two differ by the whole
        playback buffer depth. Idempotent — safe to call with nothing playing (returns 0).

        Named ``interrupt``, not ``stop``: the real sink (``AudioService``) is also a
        :class:`Service`, whose ``stop`` unwinds the mic loop — a lifecycle shutdown is a
        different act from cutting a turn's playback, and the two must not collide."""
        ...


@runtime_checkable
class FactRepository(Protocol):
    """Durable fact storage, as ``MemoryService`` needs it (SDS §8.3, §3.6, ADR-003).

    The port `SqliteFactRepo` hides behind — defined by *what the application needs* (store a
    fact, read the live ones, supersede, forget, hand out embeddings for the boot rebuild),
    never by what SQLite offers. That inversion is the whole value: the numpy matrix (§8.5),
    the FTS5 shadow, the PRAGMAs are all the adapter's business, not this contract's.

    Every method is ``async`` because ``sqlite3`` is blocking, synchronous I/O that must never
    touch the event loop (P8, §3.8.2): the adapter offloads each call to a dedicated writer
    thread, so the port is async even though SQLite itself is not. Timestamps crossing here are
    epoch **seconds**, UTC (§8.2) — the :class:`~avid.domain.Fact` convention, not the
    :class:`~avid.domain.Event` envelope's ``timestamp_ms``.
    """

    async def add(
        self,
        fact: Fact,
        *,
        embedding: bytes | None = None,
        routine: RoutineSpec | None = None,
    ) -> int:
        """Insert ``fact`` and return its assigned id.

        ``routine`` writes the fact's ``routines`` row (§8.3, §10.3) **in the same transaction**.
        §6.6 promises ``remember_fact`` is durable before it returns; a schedule that landed in a
        second transaction would make that promise half true, and the failing half is the one §10
        needs — a routine fact with no ``routines`` row is invisible to the scheduler and reports
        nothing. The database owns the id (an
        ``INTEGER PRIMARY KEY`` rowid, §8.2), so ``fact.id`` is ignored on insert and the new
        id is returned for the caller to carry. ``embedding`` is the pre-normalised 384×f32 LE
        BLOB (§8.2), crossing as opaque ``bytes`` so the port stays ``numpy``-free; ``None``
        until the embedder (#118) is wired."""
        ...

    async def get(self, fact_id: int) -> Fact | None:
        """The fact with ``fact_id``, or ``None`` if it does not exist. Never returns the
        embedding — the vector is the index's concern (§8.5), not the value's."""
        ...

    async def fetch_live(self) -> Sequence[Fact]:
        """Every live (non-superseded) fact, most-recently-accessed first — the ``idx_facts_live``
        hot path (§7.7). Superseded facts are retained for temporal reasoning (§7.8) but excluded
        here; ``retrieve()`` only ever ranks live ones."""
        ...

    async def mark_superseded(self, old_id: int, new_id: int, *, at: int) -> None:
        """Point ``old_id`` at the fact that replaced it (§7.8), setting ``superseded_by`` **and**
        ``superseded_at`` together — the schema's paired ``CHECK`` rejects setting one without the
        other. Soft supersession: the old fact is not deleted, so "what did I *used* to drink?"
        stays answerable. ``at`` is epoch seconds (§8.2)."""
        ...

    async def delete(self, fact_id: int) -> None:
        """Hard-delete ``fact_id`` — the ``forget()`` primitive (§7.10, UC-05). The schema's
        ``ON DELETE CASCADE`` (with ``foreign_keys = ON``, §8.4) removes the fact's routines and
        triggers with it; the FTS5 shadow is swept by its delete trigger. A privacy operation, so
        it leaves no trace to reason over — distinct from supersession."""
        ...

    async def load_embeddings(self) -> Sequence[tuple[int, bytes]]:
        """Every live fact's ``(id, embedding)`` for the boot-time index rebuild (§8.5): SQLite is
        truth, the numpy matrix is a write-through cache reconstructed on start. Facts without an
        embedding are skipped. The BLOB crosses as opaque ``bytes`` — turning it into the float32
        matrix is the index adapter's job (#120), keeping this port ``numpy``-free."""
        ...

    async def keyword_search(self, query: str, *, limit: int) -> Sequence[int]:
        """The live fact ids whose text keyword-matches ``query``, best first, capped at ``limit``.

        The **∪ keyword** half of §7.7's hybrid retrieval (#120): vector search alone cannot
        reliably surface a proper noun — *"Maya" embeds to mush* — so the retriever unions these
        ids with its cosine candidates before scoring. Backed by the ``facts_fts`` FTS5 shadow
        (§8.3), ranked by ``bm25``; the tokenisation and the FTS5 ``MATCH`` grammar are the
        adapter's business, so a query full of punctuation never reaches this port as a syntax
        error. Filtered to **live facts** (``superseded_by IS NULL``, §7.8) like every retrieval
        path — history stays stored but never contaminates recall. Returns ``()`` for a query with
        no searchable terms."""
        ...

    async def aclose(self) -> None:
        """Close the connection and shut the writer thread down. Idempotent."""
        ...


@runtime_checkable
class EpisodeStore(Protocol):
    """Durable raw-transcript storage, as ``EpisodeRecorder`` needs it (SDS §7.5, §8.3, ADR-003).

    The ``episodes`` table behind a port — the §7.5 debugging/reflection tier, **write-only with
    respect to the conversation flow** (AC-4): nothing in any retrieval path reads it, so a failure
    here can never affect a turn. Defined by *what the recorder needs* — key a turn's transcript by
    its ``correlation_id`` (§3.12.2), accumulate lines, count turns, and prune the old — never by
    what SQLite offers.

    Every method is ``async`` because ``sqlite3`` is blocking I/O that must never touch the event
    loop (P8, §3.8.2): the one real adapter offloads each call to a dedicated writer thread, so the
    port is async even though SQLite is not. Timestamps crossing here are epoch **seconds**, UTC
    (§8.2) — the ``Fact``/episode convention, deliberately *not* the ``Event`` envelope's
    ``timestamp_ms`` (do not cross the two). ``correlation_id`` is the key throughout; the
    single-writer serialisation is what lets the write ops be insert-if-absent-then-update without a
    ``UNIQUE`` constraint (§8.3's table has only a non-unique index), so an out-of-order event
    (§9.1.5: the bus is FIFO per subscriber, not across) still lands on the right row.
    """

    async def start_episode(self, correlation_id: UUID, *, at: int) -> None:
        """Ensure an episode row exists for ``correlation_id``, stamping ``started_at`` = ``at`` on
        creation (§7.5). Idempotent: a second call for a live episode is a no-op, so the turn origin
        need not be the first event this recorder happens to process (§9.1.5)."""
        ...

    async def append(self, correlation_id: UUID, line: str, *, at: int) -> None:
        """Append ``line`` to the episode's accumulated transcript and advance ``ended_at`` to ``at``
        (§7.5, AC-2). Creates the row if it does not yet exist (insert-if-absent), so a transcript
        line that arrives before its ``start_episode`` is never dropped."""
        ...

    async def end_turn(self, correlation_id: UUID, *, at: int) -> None:
        """Record a completed turn: increment ``turn_count`` and advance ``ended_at`` to ``at``
        (§7.5, AC-2). Creates the row if absent, like :meth:`append`."""
        ...

    async def prune(self, *, older_than: int, limit: int) -> int:
        """Delete up to ``limit`` episodes whose ``started_at`` is before ``older_than`` (epoch
        seconds), returning how many were removed — the §7.5 90-day retention. **Bounded per call**
        (AC-3): a huge backlog drains over successive passes rather than stalling the loop on one
        giant ``DELETE`` (P8). ``older_than`` and ``limit`` are injected from config (P7)."""
        ...

    async def aclose(self) -> None:
        """Close the connection and shut the writer thread down. Idempotent."""
        ...


@runtime_checkable
class TriggerRepository(Protocol):
    """The ``triggers`` and ``routines`` tables, as ``BehaviorService`` needs them (SDS §8.3, §10.3).

    Defined by what the behaviour engine does, never by what SQLite offers: turn a stored routine
    fact into a live schedule, rebuild the scheduler's heap after a restart, and record what a
    trigger has done to itself — fired, been ignored, been switched off.

    Async for the same reason :class:`EpisodeStore` is: ``sqlite3`` is blocking I/O and must never
    touch the loop (P8, §3.8.2). Timestamps are epoch **seconds**, UTC (§8.2) — never the ``Event``
    envelope's ``timestamp_ms``; do not cross the two.

    ⚠️ **Writes here are direct awaited calls, never bus events.** §4's rule: the bus carries
    notifications, not obligations, and losing a "this trigger is now disabled" would leave a
    trigger the robot has decided to stop firing still firing every morning.
    """

    async def upsert_routine_trigger(
        self, fact_id: int, *, next_fire_at: int | None, cooldown_s: int, at: int
    ) -> int:
        """Create or update the ``schedule`` trigger for ``fact_id``, returning its id (§10.3).

        Idempotent per fact: ``memory.fact_superseded`` must **move** a routine's schedule rather
        than add a second one, or "coffee at 08:00" corrected to 08:30 leaves the robot mentioning
        coffee twice every morning — §7.8's supersession arriving as a duplicate instead of an edit.

        Re-arming clears :attr:`TriggerRecord.ignore_streak` and restores ``cooldown_s``: the user
        has just restated the routine, which is the strongest possible evidence that §10.5's backoff
        was reading a stale intent rather than an unwanted one.
        """
        ...

    async def remove_for_fact(self, fact_id: int) -> None:
        """Drop the trigger for ``fact_id`` — ``memory.fact_deleted``, and UC-07's hard delete.

        Removes the ``routines`` row as well as the trigger, which is what "its schedule" means.
        Idempotent; a fact with no trigger is not an error. The ``ON DELETE CASCADE`` in §8.3
        already removes both when the *fact* goes, so this exists for the case where the fact
        survives and only its schedule should not — and **supersession is exactly that case**:
        §7.8 marks the old fact rather than deleting it, so the cascade never fires and the
        schedule would otherwise outlive the sentence it belongs to (#346)."""
        ...

    async def enabled_triggers(self) -> Sequence[TriggerRecord]:
        """Every enabled trigger with a scheduled time — the boot rebuild (§10.3).

        Exactly ``idx_triggers_due``'s partial predicate (``enabled = 1 AND next_fire_at IS NOT
        NULL``), so a trigger §10.5 switched off stays off across a restart. That is the whole
        point of persisting the streak: a backoff that resets on reboot is not a backoff."""
        ...

    async def get(self, trigger_id: int) -> TriggerRecord | None:
        """One trigger by id, or ``None`` if it has been deleted since the heap was built."""
        ...

    async def routine_for(self, fact_id: int) -> Routine | None:
        """The ``routines`` row for ``fact_id`` as a resolvable :class:`~avid.core.schedule.Routine`,
        or ``None`` if the fact carries no schedule.

        ``None`` is a normal answer, not a failure: not every routine fact has a clock time. It is
        the *caller's* job to say so out loud — a routine-kind fact with no schedule is
        indistinguishable from a user with no routines unless someone logs the difference (§6.6)."""
        ...

    async def record_fired(
        self, trigger_id: int, *, at: int, next_fire_at: int | None
    ) -> None:
        """Stamp ``last_fired_at``, increment ``fire_count``, and set the next occurrence (§10.3).

        ``next_fire_at=None`` means the rule has run out (a finite ``COUNT=``/``UNTIL=``); the row
        then falls outside ``idx_triggers_due`` and the scheduler stops considering it."""
        ...

    async def set_next_fire(self, trigger_id: int, *, next_fire_at: int | None) -> None:
        """Move a trigger's next occurrence without touching its fire history.

        Deliberately separate from :meth:`record_fired`, which also stamps ``last_fired_at`` and
        increments ``fire_count``. A **suppressed** proposal did not fire — recording it as a fire
        would corrupt rule 5's own-cooldown arithmetic and inflate the only counter that says how
        often this trigger has actually spoken.
        """
        ...

    async def set_backoff(
        self, trigger_id: int, *, ignore_streak: int, cooldown_s: int
    ) -> None:
        """Persist §10.5's backoff after a delivered turn: the new streak and doubled cooldown.

        Both columns, one call, because they change together — a streak that advanced without its
        cooldown widening is a robot that noticed it was being ignored and did nothing about it."""
        ...

    async def disable(self, trigger_id: int) -> None:
        """Switch a trigger off — §10.5's ``ignore_streak >= limit`` (``enabled = 0``).

        The caller publishes ``behavior.trigger_disabled`` alongside: §10.5 requires this be *"logged
        loudly, never silent"*, because a trigger that turned itself off is diagnostic information
        about the design and you will never learn which of your ideas were bad if it happens
        quietly."""
        ...

    async def aclose(self) -> None:
        """Close the connection and shut the writer thread down. Idempotent."""
        ...


@runtime_checkable
class ProactiveLog(Protocol):
    """R-08's instrument — the ``proactive_log`` table (SDS §8.3, §10.6).

    §10.6 is blunt about what this is for: *"This is not an audit trail. It's the only way to tune
    §10.4 without guessing."* **Every considered proposal is written, delivered or not**, with the
    vetoing rule and the utterance it would have made — because *"if `ambient_speech` vetoed 40 times
    last week, rule 4 is too aggressive. If nothing was ever suppressed, the rules are decorative,
    and without this table both look identical from the outside."*

    The two read methods exist so a :class:`~avid.domain.behavior.PolicyContext` survives a restart.
    Rules 5 and 6 are the only ones with memory, and rebuilding them from an in-process counter
    would mean a reboot at 07:00 silently resets the day's budget — a robot that becomes five times
    more talkative every time it restarts.
    """

    async def record(
        self,
        *,
        trigger_id: int | None,
        considered_at: int,
        outcome: str,
        reason: str | None,
        utterance: str | None,
    ) -> int:
        """Write one decision and return its row id.

        ``outcome`` is ``"delivered"`` or ``"suppressed"`` (§8.3's CHECK). ``reason`` must be a
        member of :data:`~avid.domain.behavior.SUPPRESSION_REASONS` — the column is spelled
        ``reason`` and the event field ``rule``, which are deliberately the same vocabulary under
        two normative names (§10.6). ``utterance`` is what it said, or would have said.

        ⚠️ That set is the six :data:`~avid.domain.behavior.POLICY_RULES` **plus**
        :data:`~avid.domain.behavior.STALE`, which is not a policy rule: the six grade the room, and
        ``stale`` grades the booking (#339). It is written here anyway, because §10.6's instrument
        must be able to distinguish a morning the robot was switched off for from a scheduler that
        stopped working — from an empty table those look identical."""
        ...

    async def set_utterance(self, log_id: int, utterance: str) -> None:
        """Record what the robot actually said, once it has actually said it (§8.3, §10.6).

        ⚠️ ``outcome='delivered'`` is written at the moment the **gate passes**, which is several
        seconds and one network round trip before any words exist. On the rig that gap was not
        theoretical: a proactive turn fired, opened a session, crashed on its first audio chunk, and
        the audit recorded ``delivered`` — then ``ignored``, because nobody replied to a robot that
        had said nothing. §10.5 would then have doubled the cooldown and, after three mornings,
        disabled the trigger for being ignored.

        So the column §8.3 provides and nothing filled is now filled, and its **absence means
        something**: a delivered row with a NULL utterance is a turn that never spoke.
        """
        ...

    async def set_reaction(self, log_id: int, reaction: str) -> None:
        """Record whether the user engaged after a delivery (§10.5).

        ``reaction`` must be a member of :data:`~avid.domain.behavior.REACTIONS` — ``"engaged"`` or
        ``"ignored"`` — which §8.3 also enforces with a ``CHECK``. Deferred rather than written
        with the row, because it is not known until the hold-open window closes, which is the same
        signal §10.5's backoff turns on.

        ⚠️ **Not calling this at all is a third, meaningful outcome.** §8.3 defines a NULL
        ``user_reaction`` as *"unknown yet"*, and two paths leave it that way on purpose: a turn
        that produced no words (#337) and a turn nobody could have answered because capture had
        stalled (#347). Neither is the user declining, and §10.5 must not count either — three
        counted ignores disable the trigger and record it as the user rejecting proactivity, which
        is the one conclusion R-08 exists to measure. A robot that was mute, or deaf, would
        otherwise conclude it was unwanted."""
        ...

    async def delivered_since(self, *, since: int) -> int:
        """How many proposals were **delivered** at or after ``since`` — rule 6's daily budget."""
        ...

    async def last_delivered_at(self) -> int | None:
        """When the last delivery was considered, or ``None`` if there has never been one — rule 5's
        global cooldown. Survives a restart, which an in-process timestamp would not."""
        ...

    async def aclose(self) -> None:
        """Close the connection and shut the writer thread down. Idempotent."""
        ...


@runtime_checkable
class Embedder(Protocol):
    """Text → semantic vector, as memory retrieval needs it (SDS §9.3, §7.4, ADR-011).

    The port behind which the embedding model hides — ``LocalMiniLmEmbedder`` (all-MiniLM-L6-v2
    via ONNX, the accepted default), ``OpenAiEmbedder`` (the §7.4 escape hatch if R-07 fires),
    or ``FakeEmbedder`` (the CI embedder and simulator, P6). Defined by *what retrieval needs* —
    one vector per string — never by the model's tensor shapes: swapping the model is a config
    line plus a re-embed migration, not a change here (§7.4).

    The vector crosses as a plain ``Sequence[float]``, **not** a ``numpy`` array: ``core`` and
    ``domain`` stay ``numpy``-free (P1), so the fake needs no third-party dependency. Packing the
    floats into the §8.2 384×f32 LE BLOB is the repository's job (:meth:`FactRepository.add`), and
    stacking them into the search matrix is the index adapter's (§8.5, #120) — both downstream of
    this port, both ``numpy``'s private business, not this contract's.
    """

    async def embed(self, text: str) -> Sequence[float]:
        """Embed ``text`` into a **pre-normalised, unit-length** vector of length
        :attr:`dimensions` (SDS §8.2).

        Normalising at the port is load-bearing: with ‖v‖ = 1, cosine similarity *is* a dot
        product, so §7.7's ranking is one matmul with no per-query normalisation pass. ``async``
        because the real adapter runs model inference it must keep off the event loop (P8); the
        fake computes in-process and returns directly."""
        ...

    @property
    def dimensions(self) -> int:
        """The fixed vector length this embedder produces (384 for MiniLM, §7.4). Checked against
        ``[memory] dimensions`` at composition (P7) so a model/config mismatch fails loudly at
        startup rather than silently corrupting an index discovered wrong only at the gate."""
        ...


@runtime_checkable
class Retriever(Protocol):
    """The memory read path + its write-through vector index, as ``MemoryService`` needs it (§7.7, §8.5).

    Promoted to a port for #122: ``MemoryService`` is a *service*, and adapters sit above services
    (P1), so it may not import the concrete ``HybridRetriever`` — it depends on this Protocol and the
    composition root injects the adapter (P2). Defined by *what the application needs* — reconcile the
    index, retrieve, find near-duplicates for a supersession check, and keep the index in step with a
    write — never by ``numpy``: the ``N×384`` matrix (§8.5) and the matmul are the adapter's business,
    so every signature here is ``numpy``-free (``Sequence[float]`` in, ``tuple[int, ...]`` out).

    ``rebuild`` / ``retrieve`` / ``similar`` are ``async`` (the store I/O and any model inference stay
    off the loop, P8); ``append`` / ``remove`` are synchronous in-memory matrix upkeep (§8.5).
    """

    async def rebuild(self) -> None:
        """Reconcile the in-memory index from the store (§8.5) — the boot reconciliation, and the only
        one. SQLite is truth, the matrix is a write-through cache; ``MemoryService.start`` calls this."""
        ...

    async def retrieve(
        self, query: str, *, correlation_id: UUID | None = None
    ) -> tuple[int, ...]:
        """The top-k live fact ids for ``query``, best first — hybrid FTS5 ∪ cosine, §7.7-scored.
        Publishes ``memory.recall_completed`` **itself** (§9.1.3), so the caller must not re-publish it.
        ``correlation_id`` is the turn this recall serves; a fresh id is minted when a recall stands
        alone."""
        ...

    async def similar(
        self, vector: Sequence[float], *, threshold: float, k: int
    ) -> tuple[int, ...]:
        """The live fact ids whose embedding cosine ≥ ``threshold``, best first, capped at ``k`` — the
        §7.8 near-duplicate search a write runs *before* deciding supersession. Pre-normalised vectors
        (§8.2) mean cosine is a dot product; publishes nothing (it is not a recall)."""
        ...

    async def match(self, query: str, *, k: int) -> tuple[RetrievalMatch, ...]:
        """The same hybrid candidates :meth:`retrieve` finds, but carrying **why** each matched (#257).

        :meth:`retrieve` returns a ranking; this returns evidence. The difference matters to exactly
        one caller so far — ``forget`` — because §7.7's combined score is min-max normalised across
        the candidate set and therefore cannot answer *"is this match good enough to delete
        something irreversibly?"*. Each :class:`~avid.domain.RetrievalMatch` carries the **raw**
        cosine and whether FTS5 hit the fact directly, and the *service* applies the policy (P2 —
        the index reports facts, it does not decide them).

        Best-first, capped at ``k``. **Publishes nothing**: a forget is not a recall, the same reason
        :meth:`similar` publishes nothing — a `memory.recall_completed` for a deletion would put a
        lie in the event catalog."""
        ...

    def append(self, fact: Fact, embedding: bytes | None) -> None:
        """Write-through: reflect a just-stored fact in the index (§8.5), **after** its row is durable —
        scoring metadata always, a matrix row when it carries an embedding."""
        ...

    def remove(self, fact_id: int) -> None:
        """Write-through: drop a fact from the index (§8.5) — the matrix half of a supersede or a
        ``forget``, called only after the fact is gone/superseded in SQLite."""
        ...


@runtime_checkable
class TextModel(Protocol):
    """A cheap, off-turn-path text model for memory-write reasoning (SDS §7.8, §9.4 catalog).

    The vendor boundary as a port (CLAUDE.md §3): its one real adapter is an OpenAI text client over
    HTTPS, its fake is ``FakeTextModel`` (the P6 simulator and §7.8's tier-1 test double). Defined by
    *what the application needs* — a supersession judgment — never by a vendor's chat-completion shapes:
    the prompt and the response parsing are the adapter's private business, so no OpenAI type crosses
    this port. Reflection (§7.9, M10) will add its own method; the port grows only as a need arrives.
    """

    async def judge_supersession(
        self, *, new_fact: str, candidates: Sequence[tuple[int, str]]
    ) -> Sequence[int]:
        """Given ``new_fact`` and its near-duplicate ``(id, text)`` ``candidates`` (§7.8 step 3), return
        the candidate ids ``new_fact`` **updates or contradicts** — a subset of the input ids, ``()``
        when none. ``async`` because the real adapter runs HTTPS inference it must keep off the loop
        (P8); the fake decides in-process. Confabulation is a bug (§7.8): return only ids genuinely
        superseded — *unknown* is a valid "not superseded", never a guess."""
        ...

    async def judge_separation(self, *, prompt: str, first: str, second: str) -> bool:
        """Were ``first`` and ``second`` — two answers to the same ``prompt`` — produced under
        **different personalities**? (§14.7's M6 row, AVID-215.)

        ⚠️ **Separation, never quality.** The question is emphatically not *"which is better"*.
        Judging quality would make the metric a taste report and drift with the judge model; the
        M6 gate's actual claim is that two configs are *distinguishable*, so that is what is asked.
        A judge that preferred one personality would score a perfectly separated pair the same as
        an identical one.

        The port grows here because §14.7 requires the judge to run behind it — no second vendor
        surface, and a fake that lets the harness's own logic be tested offline in CI while the
        live scoring never runs there."""
        ...


@runtime_checkable
class AffectTools(Protocol):
    """The `set_affect` tool, as ``ConversationService`` needs it (SDS §6.6, §6.8, AVID-214).

    The M6 gate's second clause is *"affect inferred from response drives the face **without `ai`
    importing `display`**"*, and this port is why that holds without anyone arranging it: the
    dispatcher calls this surface, ``AffectService`` publishes one ``affect.changed``,
    ``ExpressionService`` renders it, and neither end knows the other exists (§6.8, §3.6.1). The
    `import-linter` service-independence contract fails CI if ``conversation`` ever names
    ``avid.services.affect`` directly, so the Protocol is load-bearing rather than ceremonial.

    ``AffectService`` satisfies it **structurally**, with no inheritance and no edit — which is
    also why the signature is copied from that service rather than from :class:`MemoryTools`
    beside it. ``MemoryTools`` makes ``correlation_id`` optional; ``AffectService.set_affect``
    requires it, keyword-only, and is right to: an overlay with no turn behind it is a face change
    nothing can be traced to (§9.1.1 — the id is *propagated*, never minted downstream). A
    Protocol that copied the neighbour's shape would simply not be satisfied.

    The tool takes an ``Affect``, not a string, and the dispatcher does the parsing: the model's
    raw JSON is the *dispatcher's* problem (it owns turning a bad value into a tool error), while
    this port speaks the domain's vocabulary. That is the opposite of ``MemoryTools.remember_fact``
    taking ``kind: str``, and deliberately — there the validation needs the store's own
    constraint, here the enum *is* the constraint.
    """

    async def set_affect(self, affect: Affect, *, correlation_id: UUID) -> None:
        """Apply the model's Tier-2 semantic overlay (§6.8).

        **Fire-and-forget** (§6.6 classifies it so): it returns when the overlay is recorded and
        the event published, not when a face reaches glass. §6.8's whole argument is that Tier 2's
        ~400 ms is invisible *because the Tier-1 baseline is never wrong*, so blocking a turn on a
        render would import that latency into the conversation for no benefit.

        The overlay is cleared by the next Tier-1 transition, which the service already owns — that
        clearing is what stops the robot grinning through "I've finished speaking"."""
        ...


@runtime_checkable
class MemoryTools(Protocol):
    """The three memory tools, as ``ConversationService`` needs them (SDS §6.6, §7.6, ADR-004).

    Per ADR-004 the model does not own memory — it *gets tools*, and this is the surface the
    tool dispatcher calls when the model invokes one. Promoted to a port for #125:
    ``ConversationService`` is a *service* and adapters/services sit above it (P1/P5), so it may
    not import the concrete ``MemoryService`` — it depends on this Protocol and the composition
    root injects the service (P2, AC-1). Defined in the model's **tool vocabulary** — the three
    §6.6 tools verbatim — never in the store's terms: fact *construction* (text/kind/importance →
    :class:`~avid.domain.Fact`) lives behind :meth:`remember_fact`, so the conversation layer
    never touches the domain-memory record.

    ``kind`` crosses as a plain ``str`` because it arrives from the model as raw JSON; the
    implementation validates it against ``FACT_KINDS`` (the §8.3 ``CHECK`` values) and raises on a
    seventh, which the dispatcher turns into a tool error (AC-2/AC-6). Every method is ``async``
    (the store I/O and any model inference stay off the loop, P8) and takes the turn's
    ``correlation_id`` so the write/read traces back to the turn that caused it (§3.12.2).
    """

    async def remember_fact(
        self,
        text: str,
        kind: str,
        importance: int,
        *,
        schedule: RoutineSpec | None = None,
        correlation_id: UUID | None = None,
    ) -> int:
        """Store a fact the model extracted (§7.6, UC-02) and return its id — **durable before it
        returns** (§3.7.3, AC-3): the model is told "remembered" only when the row is committed.
        Runs the full §7.8 write (supersession included). Raises :class:`ValueError` on an invalid
        ``kind`` (not in ``FACT_KINDS``) or ``importance`` (outside 1–10) rather than writing a bad
        row — the dispatcher surfaces that as a tool error (AC-6).

        ``schedule`` is the model-supplied RFC 5545 half of a ``routine``-kind fact (§6.6, added at
        M10) and is what turns UC-02 into UC-03. It is validated before the write, so a rule the
        scheduler could never resolve becomes a tool error the model can correct in the same turn
        rather than a reminder that silently never fires."""
        ...

    async def recall(
        self, query: str, *, k: int = 5, correlation_id: UUID | None = None
    ) -> Sequence[Fact]:
        """The `recall` tool (§6.7 path 2, UC-05): the top facts for ``query``, best first. ``k`` is
        the model-requested cap (default 5, §7.7). Publishes ``memory.recall_completed`` via the
        retriever; a read, so it bumps no access counts here."""
        ...

    async def top_facts(self) -> Sequence[Fact]:
        """Pre-session injection (§6.7 path 1, #126): identity + active routines + recent
        high-importance facts, bounded by count **and** a token estimate (§6.7). A pure read over the
        live facts — publishes nothing (no catalogued event). ``ConversationService`` composes the
        result into the layer-4 instruction block it injects at session open."""
        ...

    async def forget(self, query: str, *, correlation_id: UUID | None = None) -> int:
        """The `forget` tool (§7.10, UC-07, AC-5): hard-delete the facts matching ``query`` and
        return the count — **durable before it returns** (losing a deletion is a privacy bug,
        §3.7.3). A rights operation, never supersession."""
        ...


@runtime_checkable
class BehaviorTools(Protocol):
    """The behaviour vocabulary the model gets (SDS §6.6, §10.4, ADR-004).

    One tool, and §10.4 is blunt about why it has to exist: *"'Leave me alone for an hour' is a
    thing people say to companions, and it must work the first time, without configuration, or rule
    1's static window carries the whole load."* A static 22:00–07:30 window cannot know about a
    meeting at eleven.

    ``ConversationService`` depends on this Protocol, never on ``BehaviorService`` (P2/P5) — the
    same shape ``AffectTools`` has, and for the same reason: the dispatcher must be able to reach
    the behaviour engine without the conversation layer knowing one exists.

    ``duration_s`` crosses as a plain number because the model's raw JSON is the *dispatcher's*
    problem — it owns turning a bad value into a tool error — while the service owns what a
    duration means. Closer to ``MemoryTools.remember_fact``'s ``kind: str`` than to
    ``AffectTools``'s enum: there is no domain type here that could carry the constraint.
    """

    async def set_quiet(self, duration_s: int, *, correlation_id: UUID) -> int:
        """Suppress proactive turns for ``duration_s``, returning the epoch second it lifts.

        The return value is what the model tells the user, so it is the *resolved instant* rather
        than an echo of the request — "until 3 o'clock" is checkable, "for an hour" is not.

        **Fire-and-forget with respect to the turn** (§6.6): it returns when the override is
        recorded, which is immediate. Nothing waits on a schedule.

        ⚠️ The override **stacks on top of** rule 1's static window rather than replacing it. "Leave
        me alone for an hour" at 21:30 must not turn into "and then you may talk at 22:30", which is
        exactly what replacing the window would do.
        """
        ...


@runtime_checkable
class Service(Protocol):
    """A use-case service, as the run loop and composition root need it (SDS §9.2).

    The four-member shape ``AffectService`` and ``ExpressionService`` already have —
    ``name`` / ``start`` / ``stop`` / ``subscriptions`` — named at last, because M4's
    :class:`~avid.services.audio.AudioService` is the first service that owns a
    background task (the mic-consume loop) and so is the first that ``lifecycle.run``
    must actually ``start``/``stop`` (AVID-72/73 deferred the Protocol precisely until a
    service needed it). Structural, so the three services satisfy it with no edit and the
    loop depends on none of them by name (P2).

    Two consumers, split by concern: the **run loop** owns the lifetime (``start``/
    ``stop`` — a purely reactive service makes both no-ops, as the existing two do), while
    the **composition root** registers the declared subscriptions before ``bus.start()``.
    ``subscriptions()`` stays on the shape — even though ``lifecycle.run`` never calls it —
    so ``main.py`` can iterate it typed rather than reaching into a concrete class.
    """

    name: str

    async def start(self) -> None:
        """Begin any owned task (e.g. the mic loop). A no-op for a reactive service."""
        ...

    async def stop(self) -> None:
        """Unwind within the §9.2 5 s budget. Idempotent; a no-op for a reactive service."""
        ...

    def subscriptions(self) -> Sequence[Subscription]:
        """Declare — not register — what this service wants to hear (SDS §9.2). The
        composition root registers these before the bus starts (P3)."""
        ...
