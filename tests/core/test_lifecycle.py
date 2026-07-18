"""The lifecycle run loop: reaches IDLE, exits 0 on shutdown (AVID-14).

Driven with a virtual clock (``FakeClock``) and the injected ``shutdown``/``ready``
seam, so the loop is exercised deterministically without a real signal — the e2e
SIGTERM path is covered by ``tests/e2e/test_boot.py``.
"""

from __future__ import annotations

import asyncio

import pytest

from avid.adapters import FakeClock
from avid.core import lifecycle
from avid.core.event_bus import AsyncioEventBus
from avid.domain import Event, SystemShuttingDown, SystemStarted


async def test_run_reaches_idle_then_exits_zero() -> None:
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    shutdown = asyncio.Event()
    ready = asyncio.Event()

    task = asyncio.create_task(
        lifecycle.run(
            bus=bus,
            clock=clock,
            adapter_health={"clock": True, "display": True},
            shutdown=shutdown,
            ready=ready,
        )
    )

    # It reaches IDLE (signals ready) without needing us to stop it.
    await asyncio.wait_for(ready.wait(), timeout=1.0)
    assert not task.done()

    # Shutting down makes it drain and return 0.
    shutdown.set()
    result = await asyncio.wait_for(task, timeout=1.0)
    assert result == 0


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
