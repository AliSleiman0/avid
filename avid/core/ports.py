"""Port contracts — what the application needs from the physical world (AVID-11).

The inner half of the hexagonal boundary (ADR-003). Each ``Protocol`` here is
defined by *what the application needs*, never by what a device offers — that
inversion is the whole value (SDS §3.9.1). Adapters in ``avid.adapters`` satisfy
these structurally; ``main.py`` alone wires which one (P2, P3).

Ports defined here (SDS §3.5.2, §3.9.1, §9.3): :class:`EventBus`, :class:`Clock`,
:class:`Camera`, :class:`Servo`, :class:`Display`, :class:`Microphone`,
:class:`Speaker`, :class:`VoiceActivityDetector`, :class:`RealtimeClient`,
:class:`TurnSink`, :class:`FactRepository`. ``Embedder`` (SDS §9.3) lands with its
adapter in a later issue.

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

from avid.core.event_bus import E, Subscription
from avid.core.hal import AudioChunk, Axis, CameraCaps, DisplayFrame, Frame
from avid.core.realtime import RealtimeEvent
from avid.domain import Event, Fact


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

    async def play(self, chunk: AudioChunk) -> None:
        """Play one chunk of synthesized audio."""
        ...

    async def play_file(self, path: Path) -> None:
        """Play a WAV from disk — the degraded-mode canned-response bank."""
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

    async def open(self) -> None:
        """Open a fresh session (instructions + tools + injected memory) — cold, no resume."""
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


@runtime_checkable
class TurnSink(Protocol):
    """The audio seam between ``ConversationService`` and ``AudioService`` (SDS §9.1.4).

    PCM is a **direct call, never a bus event** (§9.1.4): audio does not belong on an
    at-most-once bus. This port is how a turn's audio crosses ``ConvSvc ↔ AudioSvc`` without
    the two services importing each other (P5) — the conversation service reads captured mic
    frames off :meth:`mic` to forward to the model, and pushes assistant PCM down through
    :meth:`play`. Its real implementation lands with the ``AudioService`` seam (#103); the
    :class:`~avid.adapters.turn_sink.FakeTurnSink` is the simulator (P6).
    """

    def mic(self) -> AsyncIterator[AudioChunk]:
        """Yield captured mic PCM (up), mirroring :meth:`Microphone.stream` — the frames the
        conversation service forwards to the model via :meth:`RealtimeClient.send_audio`."""
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

    async def add(self, fact: Fact, *, embedding: bytes | None = None) -> int:
        """Insert ``fact`` and return its assigned id. The database owns the id (an
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

    async def aclose(self) -> None:
        """Close the connection and shut the writer thread down. Idempotent."""
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
