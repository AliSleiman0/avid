"""The M3 gate — the scripted affect sequence, as a permanent regression test (AVID-74).

PMP §5.2's M3 gate has three parts: all affects render, a scripted affect sequence plays, and
measured affect→pixel latency ≤ 150 ms. ``docs/demos/face_pi.py`` is the human-watchable,
on-Pi exerciser that proves them with real wall-clock numbers (AVID-75). This is its
in-process CI twin: it drives the same eight-affect tour through the real
:class:`~avid.core.event_bus.AsyncioEventBus` and the real
:class:`~avid.adapters.display.FakeDisplay` — no mocks, no stubs — and asserts eight distinct
faces landed, each within budget.

**Driven explicitly, like the demo.** At M3 nothing cycles the operational state through
LISTENING/THINKING/SPEAKING (that machine arrives at M4/M5), so the tour is driven by hand via
``AffectService.set_affect`` — the real Tier-2 overlay path (SDS §6.8) — not by "running the
robot." IDLE is last in the tour because ``AffectService`` boots current = IDLE and
``set_affect`` suppresses a no-op change, so a tour starting on IDLE would render seven faces,
not eight.

**Why the latency assertion is meaningful under a fake clock.** :class:`FakeClock`'s
``monotonic_ns`` does not advance on its own, so every measured latency here is exactly
``0.0`` — deterministic on any CI runner, where a real timing assertion would be flaky. That
still catches the regression that matters: ``ExpressionService`` computes latency from a
``monotonic_ns`` delta (SDS §9.1.1), and a wiring slip — subtracting wall clock, reading the
wrong stamp — would surface as a negative or absurd value that fails ``0 <= lat <= budget``.
The real-time budget itself is validated on the Pi by ``face_pi.py`` (AVID-75).

In-process and signal-free, so like ``tests/e2e/test_m0_gate.py`` it runs on every platform
including the Windows dev box. Draining is deterministic — a ``_SignallingDisplay`` sets an
``asyncio.Event`` the test awaits after each transition — never a sleep.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from avid.adapters import FakeClock, FakeDisplay
from avid.core.event_bus import AsyncioEventBus
from avid.core.hal import DisplayFrame
from avid.domain import Affect
from avid.services import AffectService, ExpressionService

# The same tour ``face_pi.py`` plays. All eight affects, IDLE last (see the module docstring).
_TOUR: tuple[Affect, ...] = (
    Affect.LISTENING,
    Affect.THINKING,
    Affect.SPEAKING,
    Affect.HAPPY,
    Affect.SAD,
    Affect.CONFUSED,
    Affect.SLEEPING,
    Affect.IDLE,
)

_LATENCY_BUDGET_MS = 150.0  # PMP §5.2 / SDS O4
# A small panel keeps each PNG encode well under P8's 50 ms slow-callback gate, so the gate
# stays green under PYTHONASYNCIODEBUG=1 (AC-7) without depending on the CI host being fast.
_RESOLUTION = (64, 48)
_DRAIN_TIMEOUT_S = 5.0  # Beyond a generous margin a worker is stuck, not slow.


class _SignallingDisplay(FakeDisplay):
    """``FakeDisplay`` plus an awaitable "a frame landed" signal (mirrors ``tests/test_main.py``).

    Instrumentation, not a second fake: it still encodes and writes a real PNG through the real
    adapter, and keeps the ``frames``/``rendered`` records the assertions read. The signal is
    needed because ``set_affect`` returns once ``affect.changed`` is *queued*, not once the bus
    worker has rendered it — asserting on the frame count before the worker runs would be a race
    that passes locally and fails on CI.
    """

    def __init__(self, *, out_dir: Path) -> None:
        super().__init__(out_dir=out_dir, resolution=_RESOLUTION)
        self.rendered_once = asyncio.Event()

    async def render(self, frame: DisplayFrame) -> None:
        await super().render(frame)
        self.rendered_once.set()


async def test_scripted_affect_tour_renders_eight_faces_in_budget(
    tmp_path: Path,
) -> None:
    """M3 gate: the eight-affect tour lands eight distinct faces, each within budget.

    The whole gate in one flow — the real bus, both real services wired as the composition root
    wires them, and the fake-that-is-the-simulator, cooperating to turn eight ``set_affect``
    calls into eight faces on glass.
    """
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    display = _SignallingDisplay(out_dir=tmp_path)
    affect = AffectService(bus=bus, clock=clock)
    expression = ExpressionService(bus=bus, display=display, clock=clock)

    # Register both services exactly as ``main._wire_services`` does: declare, then register
    # before the bus starts. AffectService's ``state.transitioned`` subscription never fires
    # here — the tour's ``set_affect`` calls are the only publisher.
    for service in (affect, expression):
        for sub in service.subscriptions():
            bus.subscribe(
                sub.event_type,
                sub.handler,
                name=sub.name,
                policy=sub.policy,
                maxsize=sub.maxsize,
            )

    latencies: list[float] = []
    async with bus:
        assert display.frames_rendered == 0
        for target in _TOUR:
            display.rendered_once.clear()
            await affect.set_affect(target, correlation_id=uuid4())
            await asyncio.wait_for(
                display.rendered_once.wait(), timeout=_DRAIN_TIMEOUT_S
            )
            assert expression.last_latency_ms is not None
            latencies.append(expression.last_latency_ms)

    # AC-5: eight frames, all distinct, each the precomputed face for its step (identity, not
    # pixel equality — comparing 460 KB buffers is both slow and the visual assertion §14.8
    # rules out; the #72 lesson).
    assert display.frames_rendered == len(_TOUR)
    assert len(display.rendered) == len(_TOUR)
    assert len({id(frame) for frame in display.rendered}) == len(_TOUR)
    for frame, target in zip(display.rendered, _TOUR, strict=True):
        assert frame is expression.face(target)

    # AC-5: every measured latency within budget. Deterministically 0.0 under FakeClock, so this
    # guards the metric *wiring* (a sign or wrong-source slip fails the lower bound); the Pi
    # validates the real-time budget (AVID-75).
    assert len(latencies) == len(_TOUR)
    assert all(0.0 <= latency <= _LATENCY_BUDGET_MS for latency in latencies)
