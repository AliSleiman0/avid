"""On-Pi face bench demo — play a scripted affect tour and measure the latency (AVID-74).

PMP §5.2's M3 gate has three parts: all affects render, a *scripted affect sequence plays*,
and *measured affect→pixel latency ≤ 150 ms*. #72 shipped ``ExpressionService`` (the drawer)
and #73 wired it into the composition root, so ``avid`` boots to an IDLE face. This script is
the missing exerciser for gate-parts two and three: it drives all eight affects through the
**real** bus and the **real** ``ExpressionService`` into whichever ``Display`` ``--config``
selects — ``FakeDisplay`` on a laptop, ``FramebufferDisplay`` on the Pi — holding each face
~2 s so a human can watch, printing per-transition affect→pixel latency, and **exiting
non-zero if the budget is blown**. That makes #75 just "run it on the Pi and tag."

**Why the tour is driven by hand, not by "running the robot."** At M3 ``state.transitioned``
has exactly one publisher and one trigger — ``StateManager`` moving BOOTING→IDLE at boot.
Nothing cycles LISTENING/THINKING/SPEAKING yet; the state machine that would arrives with
AudioService (M4) and the conversation loop (M5). So there is no running system to *watch*
emote through eight affects. The tour is instead driven explicitly via
``AffectService.set_affect`` — the real Tier-2 semantic-overlay path (SDS §6.8), the same call
the model's tool handler will make once M4 lands. The ordering of ``_TOUR`` is load-bearing:
see its comment.

**Pi-or-laptop bench tool — not application code.** Like ``hal_pi.py`` it builds its own
object graph and lives in ``docs/``, outside P3's composition root and the ``avid/`` purity
greps. It reuses the *one* display switch (``avid.main._build_display``) rather than
re-listing it, and the adapters lazy-import their Pi-only libs, so this file imports fine
off-Pi; only ``--config config/pi.toml`` (framebuffer) touches hardware. Run:

    uv run python docs/demos/face_pi.py --config config/sim.toml   # FakeDisplay, laptop
    /opt/avid/.venv/bin/python docs/demos/face_pi.py --config config/pi.toml   # panel, Pi

See ``deploy/README.md`` for the full M3 gate runbook (AVID-75).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
from uuid import uuid4

# SystemClock is the injectable real Clock (adapters name it, not "RealClock").
from avid.adapters import SystemClock
from avid.core import Clock, Display, DisplayFrame
from avid.core.config import load_config
from avid.core.event_bus import AsyncioEventBus
from avid.domain import Affect
from avid.main import _build_display
from avid.services import AffectService, ExpressionService

# The eight faces, in the order a human watches them. IDLE is deliberately **not first**:
# AffectService boots with baseline+current = IDLE, and set_affect publishes nothing when the
# blend does not move (avid/services/affect.py) — so a tour starting on IDLE would silently
# render seven faces, not eight. Every other pair is already distinct, so any order with IDLE
# after the first slot renders all eight. Keep IDLE last.
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

_LATENCY_BUDGET_MS = 150.0  # PMP §5.2 / SDS O4 — the number the milestone is graded on.
_HOLD_S = 2.0  # AC-1: hold each face long enough for a human to actually see it.
_RENDER_TIMEOUT_S = (
    5.0  # Beyond this a render is wedged, not slow — fail rather than hang.
)


class _WatchedDisplay:
    """Wraps the ``--config``-selected ``Display``, flagging each landed render (SDS §3.9.1).

    A duck-typed :class:`~avid.core.ports.Display` (P2), **not** a fake: it delegates
    ``render`` and ``resolution`` straight through to whichever real adapter ``--config``
    chose, adding only an :class:`asyncio.Event` the tour awaits. It exists because
    ``set_affect`` returns once the ``affect.changed`` event is *queued*, not once
    ``ExpressionService`` has drawn it — so the script needs a signal that the frame actually
    reached glass before it reads the latency and moves on. Never a sleep-and-hope.
    """

    def __init__(self, inner: Display) -> None:
        self._inner = inner
        self.rendered = asyncio.Event()

    @property
    def resolution(self) -> tuple[int, int]:
        return self._inner.resolution

    async def render(self, frame: DisplayFrame) -> None:
        await self._inner.render(frame)
        self.rendered.set()


async def _run(display_inner: Display, clock: Clock) -> int:
    """Play the tour once; return 0 iff all eight rendered within budget (AC-3)."""
    display = _WatchedDisplay(display_inner)
    bus = AsyncioEventBus(clock=clock)
    affect = AffectService(bus=bus, clock=clock)
    expression = ExpressionService(bus=bus, display=display, clock=clock)

    # Register both services exactly as the composition root does (main._wire_services): the
    # services *declare* subscriptions, the wiring *registers* them before the bus starts.
    # AffectService's state.transitioned subscription simply never fires here — no StateManager
    # is driving this graph; the tour is the only publisher, via set_affect.
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
    print(f"{'affect':<10} {'latency':>10}")
    print(f"{'-' * 10} {'-' * 10}")
    async with bus:
        for target in _TOUR:
            display.rendered.clear()
            await affect.set_affect(target, correlation_id=uuid4())
            try:
                await asyncio.wait_for(
                    display.rendered.wait(), timeout=_RENDER_TIMEOUT_S
                )
            except TimeoutError:
                print(f"{target.name:<10} {'NO RENDER':>10}")
                return 1
            latency = expression.last_latency_ms
            assert latency is not None  # a render just landed, so it is set
            latencies.append(latency)
            print(f"{target.name:<10} {latency:>7.1f} ms")
            await asyncio.sleep(_HOLD_S)

    worst = max(latencies)
    print(f"{'-' * 10} {'-' * 10}")
    print(
        f"min {min(latencies):.1f} ms / median {statistics.median(latencies):.1f} ms / "
        f"max {worst:.1f} ms / count {len(latencies)}"
    )
    if len(latencies) != len(_TOUR):
        print(f"FAIL: rendered {len(latencies)}/{len(_TOUR)} faces")
        return 1
    if worst > _LATENCY_BUDGET_MS:
        print(
            f"FAIL: max latency {worst:.1f} ms exceeds {_LATENCY_BUDGET_MS:.0f} ms budget"
        )
        return 1
    print(f"PASS: all {len(_TOUR)} faces within the {_LATENCY_BUDGET_MS:.0f} ms budget")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry: pick a config profile; SystemClock so the printed latencies are real."""
    parser = argparse.ArgumentParser(
        prog="face_pi",
        description="Scripted affect tour + affect→pixel latency harness (AVID-74 / M3 gate).",
    )
    parser.add_argument(
        "--config",
        default="config/sim.toml",
        help="Config profile selecting the Display adapter (default: config/sim.toml).",
    )
    args = parser.parse_args(argv)
    config = load_config(args.config)
    return asyncio.run(_run(_build_display(config), SystemClock()))


if __name__ == "__main__":
    raise SystemExit(main())
