"""Port contracts — what the application needs from the physical world (AVID-11).

The inner half of the hexagonal boundary (ADR-003). Each ``Protocol`` here is
defined by *what the application needs*, never by what a device offers — that
inversion is the whole value (SDS §3.9.1). Adapters in ``avid.adapters`` satisfy
these structurally; ``main.py`` alone wires which one (P2, P3).

Ports defined here (SDS §3.5.2, §3.9.1, §9.3): :class:`EventBus`, :class:`Clock`,
:class:`Camera`, :class:`Servo`, :class:`Display`, :class:`Microphone`,
:class:`Speaker`, :class:`VoiceActivityDetector`. ``Embedder`` (SDS §9.3) lands
with its adapter in a later issue.

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

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from avid.core.event_bus import E, Subscription
from avid.core.hal import AudioChunk, Axis, CameraCaps, DisplayFrame, Frame
from avid.domain import Event


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
