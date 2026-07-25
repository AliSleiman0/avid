"""``EpisodeStore`` adapters — the real SQLite store and its in-memory fake (#123).

Two implementations of the :class:`~avid.core.ports.EpisodeStore` port (SDS §7.5, §8.3): the
§7.5 raw-transcript tier ``main.py`` wires into ``EpisodeRecorder``. Both mirror
:mod:`avid.adapters.fact_repository` exactly — the same shared :mod:`avid.adapters.sqlite`
machinery, the same **single writer thread** (P8, §3.8.2) that keeps the blocking ``sqlite3``
off the loop *and* serialises every call (*"one writer"*), which is what lets the write ops be
insert-if-absent-then-update without a ``UNIQUE`` constraint on ``correlation_id`` — the
``episodes`` table (``0001_initial.sql``) has only a non-unique index, and a race is impossible
with a single serialising writer.

* :class:`SqliteEpisodeStore` — the durable, file-backed store. It opens a **second** connection
  to the same ``[memory] db_path`` as ``SqliteFactRepo`` (facts and episodes share one database
  file); WAL + ``busy_timeout`` (§8.4) make two writers to one file safe, and episode writes are
  low-frequency (per utterance). Migrations are idempotent (checkssummed, §8.6), so whichever
  store touches the DB first migrates it and the other sees it already applied.
* :class:`FakeEpisodeStore` — the P6 fake, and a genuine one: the **same** ``0001_initial.sql``
  against ``":memory:"``, so the schema is the database's real behaviour, not a re-implementation.

Constructed only by the composition root or a test fixture (P3). This module owns no read path
into the conversation flow — episodes are write-only with respect to a turn (§7.5, AC-4).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sqlite3 import Connection
from typing import TypeVar
from uuid import UUID

from avid.adapters.sqlite import connect, migrate
from avid.core.ports import Clock

_log = logging.getLogger("avid.adapters.episode_store")

_T = TypeVar("_T")


class SqliteEpisodeStore:
    """File-backed :class:`~avid.core.ports.EpisodeStore`, off-loop on one writer thread."""

    def __init__(self, *, db_path: str | Path, clock: Clock) -> None:
        self._db_path = str(db_path)
        self._clock = clock
        # max_workers=1: the single writer §3.8.2 asks for. It serialises every DB call and owns
        # the one Connection, so sqlite3's thread-affinity is satisfied and insert-if-absent is
        # race-free without a UNIQUE constraint on correlation_id.
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="episode-store"
        )
        self._conn: Connection | None = None  # opened lazily, on the worker thread
        self._closed = False

    # -- worker-thread helpers (never touched from the event loop) --------------

    def _conn_sync(self) -> Connection:
        """Return the connection, opening + migrating it on first use — on the writer thread, so the
        ``Connection`` is created on the one thread that will ever use it. ``migrate`` is idempotent
        (§8.6), so it is a no-op when ``SqliteFactRepo`` already migrated the shared file."""
        if self._conn is None:
            _log.debug("opening episode store at %s", self._db_path)
            conn = connect(self._db_path)
            migrate(conn, now=self._clock.now())
            self._conn = conn
        return self._conn

    async def _run(self, fn: Callable[[], _T]) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, fn)

    @staticmethod
    def _ensure(conn: Connection, corr: str, at: int) -> None:
        """Insert an episode row for ``corr`` if none exists yet (started_at = ``at``). One
        statement, atomic on the single writer — so an ``append``/``end_turn`` that arrives before
        its ``start_episode`` (bus FIFO is per-subscriber, not across — §9.1.5) still lands."""
        conn.execute(
            "INSERT INTO episodes (correlation_id, started_at, turn_count, transcript) "
            "SELECT ?, ?, 0, '' "
            "WHERE NOT EXISTS (SELECT 1 FROM episodes WHERE correlation_id = ?)",
            (corr, at, corr),
        )

    # -- the port -----------------------------------------------------------------

    async def start_episode(self, correlation_id: UUID, *, at: int) -> None:
        corr = str(correlation_id)

        def _start() -> None:
            conn = self._conn_sync()
            with conn:
                self._ensure(conn, corr, at)

        await self._run(_start)

    async def append(self, correlation_id: UUID, line: str, *, at: int) -> None:
        corr = str(correlation_id)

        def _append() -> None:
            conn = self._conn_sync()
            with conn:
                self._ensure(conn, corr, at)
                conn.execute(
                    "UPDATE episodes SET transcript = COALESCE(transcript, '') || ?, "
                    "ended_at = ? WHERE correlation_id = ?",
                    (line + "\n", at, corr),
                )

        await self._run(_append)

    async def end_turn(self, correlation_id: UUID, *, at: int) -> None:
        corr = str(correlation_id)

        def _end() -> None:
            conn = self._conn_sync()
            with conn:
                self._ensure(conn, corr, at)
                conn.execute(
                    "UPDATE episodes SET turn_count = turn_count + 1, ended_at = ? "
                    "WHERE correlation_id = ?",
                    (at, corr),
                )

        await self._run(_end)

    async def prune(self, *, older_than: int, limit: int) -> int:
        def _prune() -> int:
            conn = self._conn_sync()
            with conn:
                # Bounded by the subquery LIMIT (AC-3): plain DELETE has no LIMIT without a
                # non-default compile option, so we delete a capped id set per pass. A large
                # backlog drains over successive scheduled passes, never one loop-stalling DELETE.
                cur = conn.execute(
                    "DELETE FROM episodes WHERE id IN "
                    "(SELECT id FROM episodes WHERE started_at < ? LIMIT ?)",
                    (older_than, limit),
                )
            return cur.rowcount

        return await self._run(_prune)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True

        def _close() -> None:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

        await self._run(_close)
        self._pool.shutdown(wait=True)


class FakeEpisodeStore(SqliteEpisodeStore):
    """In-memory :class:`~avid.core.ports.EpisodeStore` — the P6 fake (SDS §3.9.2).

    Same class, same SQL, same migrations, at ``":memory:"`` — so the schema is the database's,
    not a re-implementation that could drift. The single writer thread holds the one in-memory
    connection open for the object's lifetime, so the data survives across calls.
    """

    def __init__(self, *, clock: Clock) -> None:
        super().__init__(db_path=":memory:", clock=clock)


__all__ = ["FakeEpisodeStore", "SqliteEpisodeStore"]
