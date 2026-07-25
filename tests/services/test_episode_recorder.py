"""``EpisodeRecorder`` — the write-only §7.5 transcript observer (#123).

Real :class:`AsyncioEventBus`, a real :class:`FakeEpisodeStore`, :class:`FakeClock` — no mocks
(SDS §14.3). The properties under test are *what lands in the episodes table* and *that the
recorder is an isolated observer*: a raising write can never reach another subscriber, and the
prune runs on virtual time so the 90-day retention is a millisecond test, not a quarter's wait.
The bus is FIFO **per subscriber, not across** (#72), so tests assert transcript *content* and
per-type counts, never cross-subscription line order.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from avid.adapters import FakeEpisodeStore
from avid.adapters.clock import FakeClock
from avid.core.envelope import envelope
from avid.core.event_bus import AsyncioEventBus
from avid.domain import (
    ConversationAssistantResponded,
    ConversationTurnEnded,
    ConversationTurnStarted,
    ConversationUserTranscribed,
    Event,
    SystemHandlerFailed,
    TokenUsage,
)
from avid.services.episode_recorder import EpisodeRecorder

_ONE_DAY = 86_400


class _BoomStore:
    """An :class:`~avid.core.ports.EpisodeStore` whose ``append`` always raises — the real store's
    failure modes (a full SD card, a locked DB) surface here. AC-4: it must stay contained."""

    async def start_episode(self, correlation_id: UUID, *, at: int) -> None:
        pass

    async def append(self, correlation_id: UUID, line: str, *, at: int) -> None:
        raise RuntimeError("disk full")

    async def end_turn(self, correlation_id: UUID, *, at: int) -> None:
        pass

    async def prune(self, *, older_than: int, limit: int) -> int:
        return 0

    async def aclose(self) -> None:
        pass


@contextlib.asynccontextmanager
async def _rig(
    *,
    store: Any = None,
    retention_days: int = 90,
    prune_interval_s: float = 3600.0,
    prune_batch: int = 500,
    extra_subs: tuple[tuple[type[Event], Any, str], ...] = (),
) -> AsyncIterator[tuple[FakeClock, AsyncioEventBus, Any]]:
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    the_store = store if store is not None else FakeEpisodeStore(clock=clock)
    recorder = EpisodeRecorder(
        clock=clock,
        store=the_store,
        retention_days=retention_days,
        prune_interval_s=prune_interval_s,
        prune_batch=prune_batch,
    )
    for sub in recorder.subscriptions():
        bus.subscribe(
            sub.event_type,
            sub.handler,
            name=sub.name,
            policy=sub.policy,
            maxsize=sub.maxsize,
        )
    for event_type, handler, name in extra_subs:
        bus.subscribe(event_type, handler, name=name)
    await bus.start()
    await recorder.start()
    try:
        yield clock, bus, the_store
    finally:
        await recorder.stop()
        await bus.stop()


async def _rows(store: FakeEpisodeStore) -> list[dict[str, Any]]:
    def _query() -> list[dict[str, Any]]:
        conn = store._conn_sync()
        return [
            dict(row)
            for row in conn.execute(
                "SELECT correlation_id, started_at, ended_at, turn_count, transcript "
                "FROM episodes ORDER BY id"
            ).fetchall()
        ]

    return await store._run(_query)


async def _wait_rows(
    store: FakeEpisodeStore, pred: Any, *, tries: int = 300
) -> list[dict[str, Any]]:
    """Poll the store (each read runs after pending writes on the single writer, FIFO) until
    ``pred(rows)`` holds — so a test never asserts before the recorder's writes have landed."""
    rows: list[dict[str, Any]] = []
    for _ in range(tries):
        rows = await _rows(store)
        if pred(rows):
            return rows
        await asyncio.sleep(0)
    return rows


def _env(clock: FakeClock, cid: UUID) -> Any:
    return envelope(clock=clock, correlation_id=cid, source="test")


async def _record_turn(
    bus: AsyncioEventBus,
    clock: FakeClock,
    cid: UUID,
    *,
    user_text: str,
    assistant_text: str,
    is_approximate: bool = False,
) -> None:
    await bus.publish(ConversationTurnStarted(**_env(clock, cid), initiator="user"))
    await bus.publish(
        ConversationUserTranscribed(
            **_env(clock, cid), text=user_text, is_approximate=is_approximate
        )
    )
    await bus.publish(
        ConversationAssistantResponded(
            **_env(clock, cid), text=assistant_text, item_id="item_0"
        )
    )
    await bus.publish(
        ConversationTurnEnded(
            **_env(clock, cid),
            duration_ms=1200,
            usage=TokenUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1),
        )
    )


# --- AC-1 the service shape ---------------------------------------------------------------


async def test_subscriptions_declare_the_four_conversation_facts() -> None:
    recorder = EpisodeRecorder(
        clock=FakeClock(),
        store=FakeEpisodeStore(clock=FakeClock()),
        retention_days=90,
        prune_interval_s=3600.0,
        prune_batch=500,
    )
    subs = recorder.subscriptions()
    assert recorder.name == "EpisodeRecorder"
    assert {s.event_type for s in subs} == {
        ConversationTurnStarted,
        ConversationUserTranscribed,
        ConversationAssistantResponded,
        ConversationTurnEnded,
    }
    assert {s.name for s in subs} == {
        "EpisodeRecorder.turn_started",
        "EpisodeRecorder.user_transcribed",
        "EpisodeRecorder.assistant_responded",
        "EpisodeRecorder.turn_ended",
    }


# --- AC-2 recording under the correlation_id ----------------------------------------------


async def test_a_turn_is_recorded_under_its_correlation_id() -> None:
    cid = uuid4()
    async with _rig() as (clock, bus, store):
        await _record_turn(
            bus,
            clock,
            cid,
            user_text="my name is Ali",
            assistant_text="nice to meet you",
        )
        rows = await _wait_rows(store, lambda r: r and r[0]["turn_count"] == 1)
        assert len(rows) == 1
        row = rows[0]
        assert row["correlation_id"] == str(cid)
        assert row["turn_count"] == 1  # end_turn fired
        # content, not cross-subscription order (#72): both lines present under the one id.
        assert "[user] my name is Ali" in row["transcript"]
        assert "[assistant] nice to meet you" in row["transcript"]


# --- AC-5 the is_approximate flag ---------------------------------------------------------


async def test_an_approximate_user_transcript_is_recorded_with_the_flag() -> None:
    cid = uuid4()
    async with _rig() as (clock, bus, store):
        await _record_turn(
            bus,
            clock,
            cid,
            user_text="i think my name is",
            assistant_text="go on",
            is_approximate=True,
        )
        rows = await _wait_rows(
            store, lambda r: r and "approximate" in r[0]["transcript"]
        )
        assert "[user approximate] i think my name is" in rows[0]["transcript"]


# --- AC-3 the 90-day prune ----------------------------------------------------------------


async def test_the_prune_loop_removes_episodes_past_retention() -> None:
    cid = uuid4()
    async with _rig(retention_days=1, prune_interval_s=10.0) as (clock, bus, store):
        # Record a turn at t0 (elapsed 0), then let the write land.
        await _record_turn(bus, clock, cid, user_text="old", assistant_text="reply")
        await _wait_rows(store, lambda r: bool(r))
        # Jump forward well past the 1-day retention; the prune loop (interval 10s) fires once and
        # deletes the now-stale episode (started_at < now - retention). Virtual time — instant.
        await clock.advance(2 * _ONE_DAY)
        remaining = await _wait_rows(store, lambda r: not r)
        assert remaining == []


# --- AC-4 the observer is isolated --------------------------------------------------------


async def test_a_failing_store_never_reaches_another_subscriber() -> None:
    # The recorder writes on user_transcribed and its store raises; a *separate* subscriber to the
    # same fact must still receive it, and the bus must swallow the raise into system.handler_failed
    # (§3.5.2) — a recorder failure can never affect a turn (AC-4).
    cid = uuid4()
    seen: list[Event] = []
    failures: list[Event] = []

    async def _collect(event: Event) -> None:
        seen.append(event)

    async def _collect_failure(event: Event) -> None:
        failures.append(event)

    async with _rig(
        store=_BoomStore(),
        extra_subs=(
            (ConversationUserTranscribed, _collect, "test.collect"),
            (SystemHandlerFailed, _collect_failure, "test.failure"),
        ),
    ) as (clock, bus, _store):
        await bus.publish(
            ConversationUserTranscribed(
                **_env(clock, cid), text="hello", is_approximate=False
            )
        )
        for _ in range(300):
            if seen and failures:
                break
            await asyncio.sleep(0)

        assert len(seen) == 1  # the other subscriber got the fact regardless
        assert (
            failures
        )  # the recorder's raise was swallowed + republished, not propagated
