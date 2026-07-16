"""Tests for the AVID-9 event bus core: concurrent dispatch, static subscription,
fire-and-forget publish, and correlation propagation. Each maps to an acceptance
criterion on the issue. Failure isolation and backpressure are AVID-10.
"""

import asyncio
from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID, uuid4

import pytest

from avid.core import AsyncioEventBus
from avid.domain import Event


# --- test events -----------------------------------------------------------
# Two distinct concrete event types with valid (P4) catalog names. The bus
# routes by type, not name; the names only need to pass validation.


@dataclass(frozen=True, slots=True, kw_only=True)
class TickEvent(Event):
    name: ClassVar[str] = "system.started"


@dataclass(frozen=True, slots=True, kw_only=True)
class TockEvent(Event):
    name: ClassVar[str] = "affect.changed"


def make(
    cls: type[Event] = TickEvent,
    *,
    correlation_id: UUID | None = None,
    source: str = "test",
) -> Event:
    """Construct an event with a caller-supplied identity. The bus never mints."""
    return cls(
        event_id=uuid4(),
        correlation_id=correlation_id or uuid4(),
        timestamp_ms=0,
        monotonic_ns=0,
        source=source,
    )


async def _noop(_event: Event) -> None:
    return None


# --- concurrency -----------------------------------------------------------


async def test_two_subscribers_run_concurrently() -> None:
    """Headline AC: two subscribers to one event both run, and in parallel."""
    bus = AsyncioEventBus()
    both_arrived = asyncio.Barrier(2)
    done_one = asyncio.Event()
    done_two = asyncio.Event()

    async def one(_e: Event) -> None:
        await both_arrived.wait()  # releases only if `two` also arrives
        done_one.set()

    async def two(_e: Event) -> None:
        await both_arrived.wait()
        done_two.set()

    bus.subscribe(TickEvent, one, name="one")
    bus.subscribe(TickEvent, two, name="two")

    async with bus:
        await bus.publish(make())
        # If dispatch were sequential, `one` would block at the barrier forever
        # (two never starts) and this wait_for would time out.
        await asyncio.wait_for(
            asyncio.gather(done_one.wait(), done_two.wait()), timeout=1.0
        )


async def test_publish_returns_before_handler_completes() -> None:
    """AC: publish() returns once queued, not once handled."""
    bus = AsyncioEventBus()
    release = asyncio.Event()
    handled = asyncio.Event()

    async def slow(_e: Event) -> None:
        await release.wait()
        handled.set()

    bus.subscribe(TickEvent, slow, name="slow")

    async with bus:
        await bus.publish(make())
        assert not handled.is_set()  # returned without awaiting the handler
        await asyncio.sleep(0)  # let the worker pick it up and block on `release`
        assert not handled.is_set()
        release.set()
        await asyncio.wait_for(handled.wait(), timeout=1.0)


async def test_ordered_per_publisher() -> None:
    """AC (§3.5.4): a single subscriber sees events in publish order."""
    bus = AsyncioEventBus()
    received: list[str] = []
    done = asyncio.Event()
    n = 20

    async def collect(e: Event) -> None:
        received.append(e.source)
        if len(received) == n:
            done.set()

    bus.subscribe(TickEvent, collect, name="collect")

    async with bus:
        for i in range(n):
            await bus.publish(make(source=str(i)))
        await asyncio.wait_for(done.wait(), timeout=1.0)

    assert received == [str(i) for i in range(n)]


# --- routing ---------------------------------------------------------------


async def test_dispatch_is_by_exact_type() -> None:
    """A subscriber to type A never receives type B."""
    bus = AsyncioEventBus()
    ticks: list[Event] = []
    tocks: list[Event] = []
    tick_seen = asyncio.Event()

    async def on_tick(e: Event) -> None:
        ticks.append(e)
        tick_seen.set()

    async def on_tock(e: Event) -> None:
        tocks.append(e)

    bus.subscribe(TickEvent, on_tick, name="tick")
    bus.subscribe(TockEvent, on_tock, name="tock")

    async with bus:
        await bus.publish(make(TickEvent))
        await asyncio.wait_for(tick_seen.wait(), timeout=1.0)
        await asyncio.sleep(0)  # give any errant TockEvent delivery a chance to run

    assert len(ticks) == 1
    assert tocks == []


async def test_correlation_id_is_delivered_unchanged() -> None:
    """AC: correlation_id propagates from the origin event to the subscriber."""
    bus = AsyncioEventBus()
    seen: list[UUID] = []
    got = asyncio.Event()

    async def capture(e: Event) -> None:
        seen.append(e.correlation_id)
        got.set()

    bus.subscribe(TickEvent, capture, name="capture")
    cid = uuid4()

    async with bus:
        await bus.publish(make(correlation_id=cid))
        await asyncio.wait_for(got.wait(), timeout=1.0)

    assert seen == [cid]


async def test_publish_with_no_subscribers_is_noop() -> None:
    bus = AsyncioEventBus()
    async with bus:
        await bus.publish(make(TockEvent))  # nobody listening -> returns cleanly


# --- static subscription (AC) ---------------------------------------------


def test_subscribe_requires_a_name_keyword() -> None:
    bus = AsyncioEventBus()
    with pytest.raises(TypeError):
        bus.subscribe(TickEvent, _noop)  # type: ignore[call-arg]


async def test_subscribe_rejects_empty_name() -> None:
    bus = AsyncioEventBus()
    with pytest.raises(ValueError, match="non-empty"):
        bus.subscribe(TickEvent, _noop, name="")


async def test_subscribe_after_start_raises() -> None:
    bus = AsyncioEventBus()
    async with bus:
        with pytest.raises(RuntimeError, match="static"):
            bus.subscribe(TickEvent, _noop, name="late")


# --- lifecycle -------------------------------------------------------------


async def test_publish_before_start_raises() -> None:
    bus = AsyncioEventBus()
    with pytest.raises(RuntimeError, match="before start"):
        await bus.publish(make())


async def test_double_start_raises() -> None:
    bus = AsyncioEventBus()
    await bus.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            await bus.start()
    finally:
        await bus.stop()


async def test_stop_is_idempotent() -> None:
    bus = AsyncioEventBus()
    await bus.start()
    await bus.stop()
    await bus.stop()  # second stop is a clean no-op


async def test_stop_before_start_is_noop() -> None:
    bus = AsyncioEventBus()
    await bus.stop()  # never started -> nothing to cancel
