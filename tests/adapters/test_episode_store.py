"""``EpisodeStore`` adapter — the §7.5 raw-transcript store (#123).

Driven against :class:`FakeEpisodeStore` (the P6 fake — the same ``SqliteEpisodeStore`` class at
``":memory:"``, so the SQL under test is the database's real behaviour, not a re-implementation).
No mocks (SDS §14.3). Episodes are **write-only** with respect to the conversation flow, so the
port has no read method; these tests inspect the rows through the store's own writer thread
(``_run`` + ``_conn_sync``) — the intimacy an adapter test is allowed, like ``test_main`` reading
``bus._subs``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from avid.adapters import FakeEpisodeStore
from avid.adapters.clock import FakeClock


@contextlib.asynccontextmanager
async def _store() -> AsyncIterator[FakeEpisodeStore]:
    store = FakeEpisodeStore(clock=FakeClock())
    try:
        yield store
    finally:
        await store.aclose()


async def _rows(store: FakeEpisodeStore) -> list[dict[str, Any]]:
    """Every episode row, oldest first — read on the store's writer thread (§8.3 columns)."""

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


async def test_start_append_end_accumulate_under_one_correlation_id() -> None:
    cid = uuid4()
    async with _store() as store:
        await store.start_episode(cid, at=100)
        await store.append(cid, "[user] my name is Ali", at=110)
        await store.append(cid, "[assistant] nice to meet you", at=120)
        await store.end_turn(cid, at=130)

        rows = await _rows(store)
        assert len(rows) == 1
        row = rows[0]
        assert row["correlation_id"] == str(cid)
        assert (
            row["started_at"] == 100
        )  # from start_episode, not overwritten by later writes
        assert row["ended_at"] == 130  # advanced by every write
        assert row["turn_count"] == 1  # bumped by end_turn
        assert (
            row["transcript"] == "[user] my name is Ali\n[assistant] nice to meet you\n"
        )


async def test_append_before_start_still_creates_the_row() -> None:
    # The bus is FIFO per subscriber, not across (#72), so an append can land before its
    # start_episode. Insert-if-absent means the line is never dropped.
    cid = uuid4()
    async with _store() as store:
        await store.append(cid, "[user] hello", at=110)
        rows = await _rows(store)
        assert len(rows) == 1
        assert rows[0]["started_at"] == 110  # the append's own time, as the fallback
        assert rows[0]["transcript"] == "[user] hello\n"


async def test_start_episode_is_idempotent() -> None:
    cid = uuid4()
    async with _store() as store:
        await store.start_episode(cid, at=100)
        await store.start_episode(
            cid, at=200
        )  # a second origin event must not reset the row
        rows = await _rows(store)
        assert len(rows) == 1
        assert rows[0]["started_at"] == 100


async def test_distinct_correlation_ids_are_separate_episodes() -> None:
    a, b = uuid4(), uuid4()
    async with _store() as store:
        await store.append(a, "[user] one", at=100)
        await store.append(b, "[user] two", at=100)
        rows = await _rows(store)
        assert {r["correlation_id"] for r in rows} == {str(a), str(b)}


async def test_prune_deletes_old_keeps_recent_and_is_bounded() -> None:
    async with _store() as store:
        for started in (100, 200, 300, 400, 500):
            await store.start_episode(uuid4(), at=started)

        # Bounded: at most `limit` per pass, even though 4 rows are older than the cutoff.
        first = await store.prune(older_than=450, limit=2)
        assert first == 2
        # A second pass drains the rest that are older than the cutoff.
        second = await store.prune(older_than=450, limit=10)
        assert second == 2
        # The recent one (started_at 500 ≥ cutoff) survives; nothing left to prune.
        remaining = await _rows(store)
        assert [r["started_at"] for r in remaining] == [500]
        assert await store.prune(older_than=450, limit=10) == 0


async def test_aclose_is_idempotent() -> None:
    store = FakeEpisodeStore(clock=FakeClock())
    await store.start_episode(uuid4(), at=100)
    await store.aclose()
    await store.aclose()  # second close is a no-op, never raises
