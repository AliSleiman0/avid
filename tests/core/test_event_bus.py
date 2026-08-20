"""Tests for the event bus. AVID-9: concurrent dispatch, static subscription,
fire-and-forget publish, correlation propagation. AVID-10: failure isolation
(a raising handler is swallowed + republished as ``system.handler_failed``),
backpressure (bounded queues + overflow policies), and the loop guard. Each maps
to an acceptance criterion on its issue.
"""

import asyncio
from dataclasses import dataclass
from typing import ClassVar
from uuid import UUID, uuid4

import pytest

from avid.core import AsyncioEventBus, OverflowPolicy
from avid.core.event_bus import _Runner
from avid.domain import (
    REASON_HANDLER_RAISED,
    REASON_QUEUE_OVERFLOW,
    Event,
    SystemHandlerFailed,
)

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


async def test_publish_does_not_suspend_the_caller() -> None:
    """Stronger than the test above, and load-bearing elsewhere: ``publish`` never *yields*.

    Returning "before the handler completes" would still allow it to hand control back to the
    loop. It does not — its body is a started-check plus a synchronous ``_enqueue`` — and
    :meth:`AudioService.play` leans on that: it re-checks its playback epoch **once**, after
    announcing the episode, on the basis that neither the publish nor the (uncontended) state
    transition inside that window can let a barge-in in. Pinned here rather than assumed, because
    if a later change gives ``publish`` a real await — backpressure, tracing, batching — that
    reasoning silently stops holding and AVID-174 comes back through a door nobody is watching.

    Proven by racing it against a task that sets a flag the instant it is scheduled: if ``publish``
    suspended even once, that task would run and the flag would be set before ``publish`` returns.
    """
    bus = AsyncioEventBus()
    bus.subscribe(TickEvent, lambda _e: asyncio.sleep(0), name="noop")
    yielded = False

    async def flag() -> None:
        nonlocal yielded
        yielded = True

    async with bus:
        watcher = asyncio.create_task(flag())
        await bus.publish(make())
        assert not yielded, "publish() suspended the caller"
        await watcher


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


# --- AVID-10: failure isolation --------------------------------------------


def _runner_for(bus: AsyncioEventBus, event_type: type[Event]) -> _Runner:
    """The single runner for *event_type* (tests register one subscriber per type)."""
    return bus._dispatch[event_type][0]


class _Boom(RuntimeError):
    """A distinctive handler failure so assertions can match its repr."""


async def test_raising_subscriber_does_not_kill_publisher_or_peers() -> None:
    """THE headline reliability AC (§3.5.2): one subscriber raising must not take down
    the publisher or the other subscribers. Write this test before anything else."""
    bus = AsyncioEventBus()
    peer_ran = asyncio.Event()

    async def boom(_e: Event) -> None:
        raise _Boom("handler exploded")

    async def peer(_e: Event) -> None:
        peer_ran.set()

    bus.subscribe(TickEvent, boom, name="boom")
    bus.subscribe(TickEvent, peer, name="peer")

    async with bus:
        await bus.publish(make())  # publish returns normally despite `boom`
        await asyncio.wait_for(peer_ran.wait(), timeout=1.0)  # peer still runs


async def test_raising_handler_is_republished_as_handler_failed() -> None:
    """AC: a raising handler is republished as ``system.handler_failed`` carrying the
    subscriber name, event type, reason, exc, and the failed event's correlation_id."""
    bus = AsyncioEventBus()
    seen: list[SystemHandlerFailed] = []
    got = asyncio.Event()

    async def boom(_e: Event) -> None:
        raise _Boom("kaboom")

    async def observe(e: SystemHandlerFailed) -> None:
        seen.append(e)
        got.set()

    bus.subscribe(TickEvent, boom, name="boom")
    bus.subscribe(SystemHandlerFailed, observe, name="observability")
    cid = uuid4()

    async with bus:
        await bus.publish(make(correlation_id=cid))
        await asyncio.wait_for(got.wait(), timeout=1.0)

    assert len(seen) == 1
    hf = seen[0]
    assert hf.handler == "boom"
    assert hf.event_type == "TickEvent"
    assert hf.reason == REASON_HANDLER_RAISED
    assert hf.exc is not None and "kaboom" in hf.exc
    assert hf.correlation_id == cid  # pinned to the failed event's turn
    assert hf.source == "EventBus"


async def test_worker_survives_a_raising_handler() -> None:
    """AC: the worker keeps draining after a handler raises — event #2 is delivered
    even though event #1 blew up."""
    bus = AsyncioEventBus()
    delivered: list[str] = []
    second = asyncio.Event()

    async def flaky(e: Event) -> None:
        if e.source == "first":
            raise _Boom("only the first fails")
        delivered.append(e.source)
        second.set()

    bus.subscribe(TickEvent, flaky, name="flaky")

    async with bus:
        await bus.publish(make(source="first"))
        await bus.publish(make(source="second"))
        await asyncio.wait_for(second.wait(), timeout=1.0)

    assert delivered == ["second"]


async def test_handler_failed_loop_guard() -> None:
    """AC: a handler *of* ``system.handler_failed`` that itself raises must not loop
    infinitely — the bus logs and stops, never re-emitting."""
    bus = AsyncioEventBus()
    hf_calls = 0
    entered = asyncio.Event()

    async def boom(_e: Event) -> None:
        raise _Boom("original failure")

    async def failing_observer(_e: SystemHandlerFailed) -> None:
        nonlocal hf_calls
        hf_calls += 1
        entered.set()
        raise _Boom("observer also fails")  # would loop if not guarded

    bus.subscribe(TickEvent, boom, name="boom")
    bus.subscribe(SystemHandlerFailed, failing_observer, name="failing-observer")

    async with bus:
        await bus.publish(make())
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        for _ in range(5):  # let any runaway re-emission surface
            await asyncio.sleep(0)

    assert hf_calls == 1  # invoked exactly once; the guard broke the loop


# --- AVID-10: backpressure -------------------------------------------------


async def test_default_queue_is_bounded_at_100() -> None:
    """AC: per-subscriber bounded queue, default 100."""
    bus = AsyncioEventBus()
    bus.subscribe(TickEvent, _noop, name="s")
    async with bus:
        assert _runner_for(bus, TickEvent).queue.maxsize == 100


async def test_drop_oldest_evicts_the_stale_head() -> None:
    """AC: DROP_OLDEST keeps the newest events; the oldest overflow is dropped."""
    bus = AsyncioEventBus()
    received: list[str] = []
    release = asyncio.Event()
    done = asyncio.Event()

    async def gated(e: Event) -> None:
        received.append(e.source)
        await release.wait()  # block on the first event so the queue can fill
        if len(received) == 3:
            done.set()

    bus.subscribe(
        TickEvent, gated, name="gated", policy=OverflowPolicy.DROP_OLDEST, maxsize=2
    )

    async with bus:
        await bus.publish(make(source="e1"))
        await asyncio.sleep(0)  # worker picks up e1 and blocks; queue now empty
        await bus.publish(make(source="e2"))  # queue: [e2]
        await bus.publish(make(source="e3"))  # queue: [e2, e3] (full)
        await bus.publish(make(source="e4"))  # overflow -> evict e2 -> [e3, e4]
        assert _runner_for(bus, TickEvent).overflow_count == 1
        release.set()
        await asyncio.wait_for(done.wait(), timeout=1.0)

    assert received == ["e1", "e3", "e4"]  # e2 (oldest overflow) dropped


async def test_drop_newest_discards_the_incoming_event() -> None:
    """AC: DROP_NEWEST preserves the start of an incident; the newcomer is dropped."""
    bus = AsyncioEventBus()
    received: list[str] = []
    release = asyncio.Event()
    done = asyncio.Event()

    async def gated(e: Event) -> None:
        received.append(e.source)
        await release.wait()
        if len(received) == 3:
            done.set()

    bus.subscribe(
        TickEvent, gated, name="gated", policy=OverflowPolicy.DROP_NEWEST, maxsize=2
    )

    async with bus:
        await bus.publish(make(source="e1"))
        await asyncio.sleep(0)  # worker takes e1 and blocks
        await bus.publish(make(source="e2"))  # queue: [e2]
        await bus.publish(make(source="e3"))  # queue: [e2, e3] (full)
        await bus.publish(make(source="e4"))  # overflow -> discard e4
        assert _runner_for(bus, TickEvent).overflow_count == 1
        release.set()
        await asyncio.wait_for(done.wait(), timeout=1.0)

    assert received == ["e1", "e2", "e3"]  # e4 (newest) dropped


async def test_overflow_is_republished_as_handler_failed() -> None:
    """AC: overflow publishes ``system.handler_failed`` with ``reason="queue_overflow"``
    and increments a counter (loud drops, never silent)."""
    bus = AsyncioEventBus()
    release = asyncio.Event()
    overflow = asyncio.Event()
    seen: list[SystemHandlerFailed] = []

    async def gated(_e: Event) -> None:
        await release.wait()

    async def observe(e: SystemHandlerFailed) -> None:
        seen.append(e)
        overflow.set()

    bus.subscribe(
        TickEvent, gated, name="gated", policy=OverflowPolicy.DROP_OLDEST, maxsize=1
    )
    bus.subscribe(SystemHandlerFailed, observe, name="observability")

    async with bus:
        await bus.publish(make(source="e1"))
        await asyncio.sleep(0)  # worker takes e1 and blocks
        await bus.publish(make(source="e2"))  # queue: [e2] (full)
        await bus.publish(make(source="e3"))  # overflow
        await asyncio.wait_for(overflow.wait(), timeout=1.0)
        release.set()

    assert _runner_for(bus, TickEvent).overflow_count == 1
    assert len(seen) == 1
    assert seen[0].reason == REASON_QUEUE_OVERFLOW
    assert seen[0].exc is None
    assert seen[0].event_type == "TickEvent"


# --- AVID-10: registration validation --------------------------------------


def test_subscribe_rejects_block_policy() -> None:
    bus = AsyncioEventBus()
    with pytest.raises(ValueError, match="BLOCK is forbidden"):
        bus.subscribe(TickEvent, _noop, name="blocker", policy=OverflowPolicy.BLOCK)


def test_subscribe_rejects_non_positive_maxsize() -> None:
    bus = AsyncioEventBus()
    with pytest.raises(ValueError, match="maxsize"):
        bus.subscribe(TickEvent, _noop, name="tiny", maxsize=0)


async def test_queue_stats_reports_depth_capacity_and_drops_per_subscriber() -> None:
    """#380 — overflow is the failure a 30-day soak exists to surface.

    §3.5's rule is that silent drops are a debugging catastrophe and loud drops are a tuning
    signal. Until this existed, "loud" meant one log line per drop: visible while someone was
    watching, invisible over a month. ``dropped`` is monotonic for the process's life, which is
    what makes it readable *afterwards* rather than only live.
    """
    bus = AsyncioEventBus()
    release = asyncio.Event()

    async def gated(_: Event) -> None:
        await release.wait()  # block on the first event so the queue can fill

    bus.subscribe(
        TickEvent, gated, name="gated", policy=OverflowPolicy.DROP_OLDEST, maxsize=2
    )

    # Readable before start(), so a boot-time snapshot describes the declared graph rather than
    # raising on a bus that has not run yet.
    assert bus.queue_stats() == {"gated": {"depth": 0, "maxsize": 2, "dropped": 0}}

    async with bus:
        await bus.publish(make(source="e1"))
        await asyncio.sleep(0)  # worker picks up e1 and blocks; queue now empty
        await bus.publish(make(source="e2"))
        await bus.publish(make(source="e3"))  # queue full
        await bus.publish(make(source="e4"))  # overflow -> one drop
        stats = bus.queue_stats()["gated"]
        assert stats["dropped"] == 1
        assert stats["depth"] == 2
        assert stats["maxsize"] == 2
        release.set()
