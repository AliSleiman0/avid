"""The lifecycle run loop: IDLE, supervision, clean exit (AVID-14, AVID-38, AVID-40).

Driven with a virtual clock (``FakeClock``) and the injected ``shutdown``/``ready``
seam, so the loop — including the sd_notify handshake and the watchdog ping cadence —
is exercised deterministically without a real signal, a real socket, or a real second.
The e2e SIGTERM/subprocess path is covered by ``tests/e2e/test_supervision.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from avid.adapters import FakeClock, FakeServiceNotifier
from avid.core import lifecycle
from avid.core.event_bus import AsyncioEventBus
from avid.domain import Event, SystemShuttingDown, SystemStarted


class _RecordingSurface:
    """A stand-in :class:`~avid.core.lifecycle.ControlSurface` that records its
    start/stop calls — the health server's role, without binding a socket."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")


async def _wait_until_parked(clock: FakeClock) -> None:
    """Yield until the watchdog pinger has registered its sleeper on *clock*, so a
    following ``advance`` actually crosses a live deadline. Without this, the ping task
    may not have reached its first ``clock.sleep`` yet and the advance would no-op."""
    for _ in range(100):
        if clock._sleepers:
            return
        await asyncio.sleep(0)
    raise AssertionError("watchdog pinger never parked on the clock")


async def test_run_reaches_idle_then_exits_zero() -> None:
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    notifier = FakeServiceNotifier()
    shutdown = asyncio.Event()
    ready = asyncio.Event()

    task = asyncio.create_task(
        lifecycle.run(
            bus=bus,
            clock=clock,
            adapter_health={"clock": True, "display": True},
            notifier=notifier,
            watchdog_interval_s=0,  # no pings — this test is about boot + exit
            shutdown=shutdown,
            ready=ready,
        )
    )

    # It reaches IDLE (signals ready) without needing us to stop it, and has already
    # told the supervisor it is up by the time ready is set.
    await asyncio.wait_for(ready.wait(), timeout=1.0)
    assert not task.done()
    assert notifier.notifications == ["READY"]

    # Shutting down makes it drain and return 0.
    shutdown.set()
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result == 0
    # STOPPING is announced exactly once, on the way out, after READY.
    assert notifier.notifications == ["READY", "STOPPING"]


async def test_run_publishes_started_and_shutting_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    published: list[Event] = []
    original_publish = bus.publish

    async def _spy(event: Event) -> None:
        published.append(event)
        await original_publish(event)

    monkeypatch.setattr(bus, "publish", _spy)

    shutdown = asyncio.Event()
    ready = asyncio.Event()
    task = asyncio.create_task(
        lifecycle.run(
            bus=bus,
            clock=clock,
            adapter_health={"display": True},
            notifier=FakeServiceNotifier(),
            watchdog_interval_s=0,
            shutdown=shutdown,
            ready=ready,
        )
    )
    await asyncio.wait_for(ready.wait(), timeout=1.0)
    shutdown.set()
    await asyncio.wait_for(task, timeout=1.0)

    started = [e for e in published if isinstance(e, SystemStarted)]
    stopping = [e for e in published if isinstance(e, SystemShuttingDown)]
    assert len(started) == 1
    assert started[0].adapters == {"display": True}
    assert started[0].name == "system.started"
    assert len(stopping) == 1
    assert stopping[0].reason == "signal"
    # Envelope is stamped from the injected clock (epoch seconds -> ms).
    assert started[0].timestamp_ms == clock.now() * 1000


async def test_health_surface_started_before_ready_and_stopped_on_exit() -> None:
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    health = _RecordingSurface()
    shutdown = asyncio.Event()
    ready = asyncio.Event()

    task = asyncio.create_task(
        lifecycle.run(
            bus=bus,
            clock=clock,
            adapter_health={"display": True},
            notifier=FakeServiceNotifier(),
            health=health,
            watchdog_interval_s=0,
            shutdown=shutdown,
            ready=ready,
        )
    )
    await asyncio.wait_for(ready.wait(), timeout=1.0)
    # Serving before we announce readiness — /health answers the moment we are up.
    assert health.calls == ["start"]

    shutdown.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert health.calls == ["start", "stop"]


async def test_watchdog_pings_on_the_configured_interval() -> None:
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    notifier = FakeServiceNotifier()
    shutdown = asyncio.Event()
    ready = asyncio.Event()

    task = asyncio.create_task(
        lifecycle.run(
            bus=bus,
            clock=clock,
            adapter_health={"display": True},
            notifier=notifier,
            watchdog_interval_s=10.0,
            shutdown=shutdown,
            ready=ready,
        )
    )
    await asyncio.wait_for(ready.wait(), timeout=1.0)
    assert notifier.notifications == ["READY"]

    # Each advance past the interval wakes one ping. Virtual time: no real waiting.
    await _wait_until_parked(clock)
    await clock.advance(10)
    assert notifier.notifications == ["READY", "WATCHDOG"]

    await _wait_until_parked(clock)
    await clock.advance(10)
    assert notifier.notifications == ["READY", "WATCHDOG", "WATCHDOG"]

    shutdown.set()
    await asyncio.wait_for(task, timeout=1.0)
    # The pinger is cancelled cleanly and STOPPING lands last.
    assert notifier.notifications == ["READY", "WATCHDOG", "WATCHDOG", "STOPPING"]


async def test_no_watchdog_pings_when_interval_non_positive() -> None:
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    notifier = FakeServiceNotifier()
    shutdown = asyncio.Event()
    ready = asyncio.Event()

    task = asyncio.create_task(
        lifecycle.run(
            bus=bus,
            clock=clock,
            adapter_health={"display": True},
            notifier=notifier,
            watchdog_interval_s=0,
            shutdown=shutdown,
            ready=ready,
        )
    )
    await asyncio.wait_for(ready.wait(), timeout=1.0)
    await clock.advance(3600)  # an hour of virtual time
    await asyncio.sleep(0)
    assert notifier.notifications == ["READY"]  # nothing pinged

    shutdown.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert notifier.notifications == ["READY", "STOPPING"]
