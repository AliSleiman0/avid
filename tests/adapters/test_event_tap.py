"""The SSE tap and the live event feed (#385, SDS §9.5, §3.5.1).

Two halves, and the second is where the acceptance criteria live.

The **tap** half is pure fan-out and needs no socket: attach, publish, drop, detach. The
**stream** half runs against a real server on an ephemeral port, like the rest of
``tests/adapters/test_health.py``, because the properties that matter — a slow client not
perturbing the robot, a vanished client being cleaned up, shutdown not hanging on an open stream —
are properties of the socket path and not of the queue.

⚠️ The test worth reading is :func:`test_a_slow_client_is_told_what_it_missed`. AC-3 asks that a
slow consumer cannot become a back-pressure source, which is easy; §3.5.5 also says silent drops
are a debugging catastrophe, and a silent drop *inside the debugger* is that catastrophe committed
by the tool you reached for to diagnose it. Both are asserted, because passing the first alone
would be a tap that quietly lies about what happened.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from avid.adapters import HealthServer
from avid.adapters.event_tap import EventTap, event_types, render
from avid.core.event_bus import OverflowPolicy
from avid.domain import Affect, AffectChanged, Event, SystemStarted


def _event(source: str = "Test") -> SystemStarted:
    return SystemStarted(
        event_id=uuid4(),
        correlation_id=uuid4(),
        timestamp_ms=1,
        monotonic_ns=2,
        source=source,
        adapters={},
    )


# ── the catalogue ──────────────────────────────────────────────────────────────────────────


def test_the_tap_hears_every_domain_event_type() -> None:
    """Derived, not curated (AC-2's precondition).

    An event class missing from this set is invisible in exactly the situation the tap exists
    for — you would be watching the feed for the event that never arrives, and it would never
    arrive because nothing subscribed to it.
    """
    types = set(event_types())
    assert len(types) > 20, (
        f"only {len(types)} event types found — the walk has gone blind"
    )
    # A sample from four different domain modules: if the walk ever silently narrowed to one
    # module, a spot check of one type would not notice.
    assert {SystemStarted, AffectChanged} <= types
    names = {cls.__name__ for cls in types}
    assert {"AudioSpeechStarted", "MemoryFactStored", "VisionPresenceGained"} <= names


def test_an_event_class_outside_the_domain_is_not_tapped() -> None:
    """The tap taps the domain's catalogue, not whatever happens to be imported.

    Without this scoping, a test defining its own ``Event`` subclass changes the tap's subscriber
    set — so the graph would depend on which tests had run, which is how a subscriber-graph
    assertion becomes flaky rather than false.
    """

    class _NotADomainEvent(Event):
        pass

    assert _NotADomainEvent not in set(event_types())


def test_every_subscription_is_named_and_drops_the_oldest() -> None:
    """AC-3, at the bus edge. ``BLOCK`` is forbidden (§3.5.5) and would push back-pressure from a
    debugging tap into the audio path; ``name=`` is what makes the tap visible to §9.1.5's drift
    check rather than an anonymous listener nobody can enumerate."""
    subscriptions = EventTap().subscriptions()
    assert len(subscriptions) == len(event_types())
    for subscription in subscriptions:
        assert subscription.policy is OverflowPolicy.DROP_OLDEST
        assert subscription.name.startswith("EventTap.")
        assert subscription.name != "EventTap."


# ── fan-out ────────────────────────────────────────────────────────────────────────────────


async def test_an_attached_client_receives_published_events() -> None:
    tap = EventTap()
    client = tap.attach()
    await tap._on_event(_event())
    assert client.queue.qsize() == 1


async def test_with_no_client_attached_the_handler_does_nothing() -> None:
    """Why it is acceptable to subscribe to every event type unconditionally: at rest the tap
    costs one empty iteration per event."""
    tap = EventTap()
    await tap._on_event(_event())  # must not raise
    assert tap.client_count == 0


async def test_two_clients_each_get_their_own_copy() -> None:
    tap = EventTap()
    first, second = tap.attach(), tap.attach()
    await tap._on_event(_event())
    assert first.queue.qsize() == 1
    assert second.queue.qsize() == 1


async def test_a_full_client_queue_drops_the_oldest_and_counts_it() -> None:
    """AC-3: the robot is never slowed by a reader that stopped reading."""
    tap = EventTap(client_queue=2)
    client = tap.attach()
    for index in range(5):
        await tap._on_event(_event(source=f"s{index}"))
    assert client.queue.qsize() == 2
    assert client.dropped == 3
    # Newest wins: the two survivors are the last two published.
    kept = [client.queue.get_nowait().source, client.queue.get_nowait().source]
    assert kept == ["s3", "s4"]


def test_detach_is_idempotent() -> None:
    """A stream can end twice — cancelled *and* errored — and the second unwind must not raise."""
    tap = EventTap()
    client = tap.attach()
    tap.detach(client)
    tap.detach(client)
    assert tap.client_count == 0


def test_a_client_queue_of_zero_is_refused() -> None:
    with pytest.raises(ValueError, match="client_queue"):
        EventTap(client_queue=0)


# ── the stream, over a real socket ─────────────────────────────────────────────────────────


async def _open_stream(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET /events/stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
    await writer.drain()
    return reader, writer


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)


async def test_the_stream_answers_with_an_sse_head() -> None:
    tap = EventTap()
    server = HealthServer(bind="127.0.0.1", port=0, tap=tap, keepalive_s=0.05)
    await server.start()
    try:
        reader, writer = await _open_stream(server.bound_port)
        head = await _read_head(reader)
        assert b"200 OK" in head
        assert b"text/event-stream" in head
        writer.close()
    finally:
        await server.stop()


async def test_a_published_event_reaches_the_stream() -> None:
    """AC-2, end to end: the frame a `curl -N` would print."""
    tap = EventTap()
    server = HealthServer(bind="127.0.0.1", port=0, tap=tap, keepalive_s=5)
    await server.start()
    try:
        reader, writer = await _open_stream(server.bound_port)
        await _read_head(reader)
        # No polling needed: the handler attaches BEFORE writing the head, so a client that has
        # read the head is by construction already attached.
        assert tap.client_count == 1
        await tap._on_event(_event(source="AudioService"))
        frame = await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=2)
        assert b"event: system.started" in frame
        assert b'"t": "AudioService"' in frame
        writer.close()
    finally:
        await server.stop()


async def test_a_slow_client_is_told_what_it_missed() -> None:
    """AC-3, and the half that is easy to leave out.

    The queue is filled while nothing reads it, so the drop path runs; then one more event is
    published and the stream is read. The client must be told the count **before** the next
    event, so the gap appears where it happened rather than as an unexplained jump.
    """
    tap = EventTap(client_queue=1)
    server = HealthServer(bind="127.0.0.1", port=0, tap=tap, keepalive_s=5)
    await server.start()
    try:
        reader, writer = await _open_stream(server.bound_port)
        await _read_head(reader)
        assert tap.client_count == 1
        client = next(iter(tap._clients))
        # Publish more than the queue holds without letting the pump run.
        for index in range(4):
            await tap._on_event(_event(source=f"s{index}"))
        assert client.dropped >= 1
        notice = await asyncio.wait_for(reader.readuntil(b"\n\n"), timeout=2)
        assert notice.startswith(b": dropped ")
        assert b"behind" in notice
        writer.close()
    finally:
        await server.stop()


async def test_a_disconnected_client_is_detached() -> None:
    """AC-4: a stream left open for hours must not leak, and neither must one hung up on."""
    tap = EventTap()
    server = HealthServer(bind="127.0.0.1", port=0, tap=tap, keepalive_s=0.02)
    await server.start()
    try:
        reader, writer = await _open_stream(server.bound_port)
        await _read_head(reader)
        assert tap.client_count == 1
        writer.close()
        await writer.wait_closed()
        # The keepalive is what discovers the dead socket on an idle robot — nothing is being
        # published here, which is exactly the case a reader-driven design would never notice.
        # Polled rather than awaited on purpose: the condition is the server task noticing a
        # closed socket, which is not an event this test owns and cannot be signalled to it.
        for _ in range(200):  # noqa: ASYNC110 - waiting on a socket, not on an in-process event
            if tap.client_count == 0:
                break
            await asyncio.sleep(0.02)
        assert tap.client_count == 0
    finally:
        await server.stop()


async def test_stop_does_not_wait_for_an_open_stream() -> None:
    """⚠️ Without cancelling in-flight streams, ``server.wait_closed()`` waits for the connection
    and a single `curl -N` holds shutdown past systemd's 5 s budget (SDS §9.2) — a debugging
    endpoint taking the robot's shutdown down with it."""
    tap = EventTap()
    server = HealthServer(bind="127.0.0.1", port=0, tap=tap, keepalive_s=30)
    await server.start()
    reader, writer = await _open_stream(server.bound_port)
    await _read_head(reader)
    assert tap.client_count == 1
    await asyncio.wait_for(server.stop(), timeout=2)
    assert tap.client_count == 0
    writer.close()


async def test_the_stream_is_503_when_no_tap_is_wired() -> None:
    """An unwired tap answers plainly rather than pretending an empty feed is a quiet robot."""
    server = HealthServer(bind="127.0.0.1", port=0)
    await server.start()
    try:
        reader, writer = await _open_stream(server.bound_port)
        head = await _read_head(reader)
        assert b"503" in head
        writer.close()
    finally:
        await server.stop()


def test_render_carries_the_fields_the_spec_greps_for() -> None:
    """§9.5's own example is ``jq -c '{t:.source,e:.type}'`` — the source and the event name."""
    frame = render(
        AffectChanged(
            event_id=uuid4(),
            correlation_id=uuid4(),
            timestamp_ms=7,
            monotonic_ns=8,
            source="AffectService",
            affect=Affect.HAPPY,
            tier=1,
            previous=Affect.IDLE,
        )
    )
    assert b"event: affect.changed" in frame
    assert b'"t": "AffectService"' in frame
    assert frame.endswith(b"\n\n")
