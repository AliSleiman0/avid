"""``FactRepository`` adapters — the real SQLite store and its in-memory fake (#117).

Two implementations of the :class:`~avid.core.ports.FactRepository` port (SDS §8.3, §3.6):

* :class:`SqliteFactRepo` — the durable store `main.py` will wire into ``MemoryService``
  (#122). It hides the schema, the §8.4 PRAGMAs, and the migration runner behind the port's
  vocabulary. ``sqlite3`` is blocking, synchronous I/O, so **every** call is offloaded to a
  single dedicated writer thread (P8, §3.8.2): one worker both keeps the loop clean and
  serialises all access — *"one writer"* — which also keeps the ``Connection`` thread-affine,
  sidestepping ``check_same_thread``.
* :class:`FakeFactRepository` — the P6 fake, and a genuine one: it runs the **same**
  ``0001_initial.sql`` against ``":memory:"``, so the cascade, the supersession ``CHECK``, and
  the FTS5 shadow are the database's *real* behaviour, not Python re-implementing them. A
  repository has no device to simulate; an in-memory DB *is* the simulator (SDS §3.9.2), which
  is why it cannot drift from the real thing — it *is* the real thing, at a different path.

Both are constructed only by the composition root or a test fixture (P3). The embedding BLOB
crosses as opaque ``bytes`` in and out; turning it into the numpy matrix (§8.5) is the index
adapter's job (#120), so nothing here imports ``numpy``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sqlite3 import Connection, Row
from typing import TypeVar
from uuid import UUID

from avid.adapters.sqlite import connect, migrate
from avid.core.ports import Clock
from avid.domain import Fact, RoutineSpec

_log = logging.getLogger("avid.adapters.fact_repository")

_T = TypeVar("_T")

# The Fact columns, in one place so the SELECTs and the row mapper cannot drift. The embedding
# BLOB is deliberately absent — the vector belongs to the index (§8.5), never to the value.
_FACT_COLUMNS = (
    "id, text, kind, importance, confidence, created_at, last_accessed_at, "
    "access_count, superseded_by, superseded_at, derived_from, source_correlation_id"
)

# FTS5 gives ``"``, ``*``, ``(``, ``:``, ``AND``/``OR``/``NOT`` special meaning, so a raw user
# query ("What's Maya's number?") would surface as a MATCH syntax error, not a miss (§8.3). We
# reduce the query to its word tokens, quote each so it is a literal FTS5 string, and OR them:
# the keyword branch is meant to *widen* the candidate pool (recall), and §7.7's scoring — not
# this SQL — decides final ordering. A token-less query yields no MATCH string, i.e. no rows.
_WORD = re.compile(r"\w+", re.UNICODE)


def _fts_match(query: str) -> str | None:
    """Turn a free-text query into a safe FTS5 ``MATCH`` string, or ``None`` if it has no terms."""
    tokens = _WORD.findall(query)
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in tokens)


def _row_to_fact(row: Row) -> Fact:
    """Map a ``sqlite3.Row`` back to the domain :class:`Fact` (§8.3 columns → the value).

    ``derived_from`` is stored as a JSON array of ids and ``source_correlation_id`` as its
    canonical string; both are decoded here. ``kind`` restores to :data:`~avid.domain.FactKind`
    — the schema's ``CHECK`` guarantees it is one of the six, so no validation is repeated. The
    embedding is not selected: the vector is the index's concern (§8.5), not the value's.
    """
    return Fact(
        id=int(row["id"]),
        text=str(row["text"]),
        kind=row["kind"],
        importance=int(row["importance"]),
        confidence=float(row["confidence"]),
        created_at=int(row["created_at"]),
        last_accessed_at=int(row["last_accessed_at"]),
        access_count=int(row["access_count"]),
        superseded_by=None
        if row["superseded_by"] is None
        else int(row["superseded_by"]),
        superseded_at=None
        if row["superseded_at"] is None
        else int(row["superseded_at"]),
        derived_from=()
        if row["derived_from"] is None
        else tuple(int(x) for x in json.loads(row["derived_from"])),
        source_correlation_id=None
        if row["source_correlation_id"] is None
        else UUID(row["source_correlation_id"]),
    )


class SqliteFactRepo:
    """File-backed :class:`~avid.core.ports.FactRepository`, off-loop on one writer thread."""

    def __init__(self, *, db_path: str | Path, clock: Clock) -> None:
        self._db_path = str(db_path)
        self._clock = clock
        # max_workers=1: the single writer §3.8.2 asks for. It serialises every DB call and
        # owns the one Connection, so sqlite3's thread-affinity is satisfied for free.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fact-repo")
        self._conn: Connection | None = None  # opened lazily, on the worker thread
        self._closed = False

    # -- worker-thread helpers (never touched from the event loop) --------------

    def _conn_sync(self) -> Connection:
        """Return the connection, opening + migrating it on first use — on the writer thread,
        so the ``Connection`` is created on the one thread that will ever use it."""
        if self._conn is None:
            _log.debug("opening fact store at %s", self._db_path)
            conn = connect(self._db_path)
            migrate(conn, now=self._clock.now())
            self._conn = conn
        return self._conn

    async def _run(self, fn: Callable[[], _T]) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, fn)

    # -- the port -----------------------------------------------------------------

    async def add(
        self,
        fact: Fact,
        *,
        embedding: bytes | None = None,
        routine: RoutineSpec | None = None,
    ) -> int:
        derived = json.dumps(list(fact.derived_from)) if fact.derived_from else None
        corr = (
            None
            if fact.source_correlation_id is None
            else str(fact.source_correlation_id)
        )

        def _add() -> int:
            conn = self._conn_sync()
            with conn:
                cur = conn.execute(
                    "INSERT INTO facts (text, kind, importance, confidence, embedding, "
                    "created_at, last_accessed_at, access_count, superseded_by, "
                    "superseded_at, derived_from, source_correlation_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        fact.text,
                        fact.kind,
                        fact.importance,
                        fact.confidence,
                        embedding,
                        fact.created_at,
                        fact.last_accessed_at,
                        fact.access_count,
                        fact.superseded_by,
                        fact.superseded_at,
                        derived,
                        corr,
                    ),
                )
                rowid = cur.lastrowid
                assert rowid is not None  # an INSERT always assigns the rowid
                if routine is not None:
                    # Same `with conn:` block, so same transaction. §6.6 promises remember_fact is
                    # "durable before it returns"; a schedule that landed in a second transaction
                    # would make that promise half true, and the half that fails is the one §10
                    # needs — a routine fact with no routines row is a fact the scheduler cannot
                    # see and nothing reports.
                    conn.execute(
                        "INSERT INTO routines (fact_id, rrule, local_time, timezone, lead_time_s) "
                        "VALUES (?,?,?,?,?)",
                        (
                            rowid,
                            routine.rrule,
                            routine.local_time,
                            routine.timezone,
                            routine.lead_time_s,
                        ),
                    )
            return rowid

        return await self._run(_add)

    async def get(self, fact_id: int) -> Fact | None:
        def _get() -> Fact | None:
            conn = self._conn_sync()
            row = conn.execute(
                f"SELECT {_FACT_COLUMNS} FROM facts WHERE id = ?", (fact_id,)
            ).fetchone()
            return None if row is None else _row_to_fact(row)

        return await self._run(_get)

    async def fetch_live(self) -> Sequence[Fact]:
        def _fetch() -> list[Fact]:
            conn = self._conn_sync()
            rows = conn.execute(
                f"SELECT {_FACT_COLUMNS} FROM facts "
                "WHERE superseded_by IS NULL ORDER BY last_accessed_at DESC"
            ).fetchall()
            return [_row_to_fact(row) for row in rows]

        return await self._run(_fetch)

    async def fetch_all(self) -> Sequence[Fact]:
        """Every fact including superseded ones, oldest first — §7.10's audit (#386).

        Deliberately a sibling of :meth:`fetch_live` rather than a parameter on it: the two answer
        different questions, in different orders, for different callers, and one of them is on the
        §7.7 ranking hot path while the other is read by a person once.
        """

        def _fetch() -> list[Fact]:
            conn = self._conn_sync()
            rows = conn.execute(
                f"SELECT {_FACT_COLUMNS} FROM facts ORDER BY created_at ASC, id ASC"
            ).fetchall()
            return [_row_to_fact(row) for row in rows]

        return await self._run(_fetch)

    async def mark_superseded(self, old_id: int, new_id: int, *, at: int) -> None:
        def _mark() -> None:
            conn = self._conn_sync()
            with conn:
                conn.execute(
                    "UPDATE facts SET superseded_by = ?, superseded_at = ? WHERE id = ?",
                    (new_id, at, old_id),
                )

        await self._run(_mark)

    async def delete(self, fact_id: int) -> None:
        def _delete() -> None:
            conn = self._conn_sync()
            with conn:
                # Clear the supersession pointer PAIR on any fact this one replaced, first and in
                # the same transaction. The §8.3 schema declares `superseded_by ... ON DELETE SET
                # NULL` alongside `CHECK ((superseded_by IS NULL) = (superseded_at IS NULL))`, and
                # the two disagree: SET NULL nulls the pointer and leaves the timestamp, so SQLite
                # rejects its own cascade and the DELETE raises IntegrityError. Reachable from one
                # ordinary sequence — state a fact, contradict it, then ask to forget the newer
                # one — and it raises inside a tool handler, where it abandons the whole write.
                #
                # Clearing both columns completes what SET NULL was chosen to mean: if the fact
                # that replaced it is gone, the older fact is no longer superseded by anything and
                # returns to live retrieval. The alternative, cascading the delete, would destroy
                # a fact the user never asked to forget (§7.10).
                conn.execute(
                    "UPDATE facts SET superseded_by = NULL, superseded_at = NULL "
                    "WHERE superseded_by = ?",
                    (fact_id,),
                )
                conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))

        await self._run(_delete)

    async def load_embeddings(self) -> Sequence[tuple[int, bytes]]:
        def _load() -> list[tuple[int, bytes]]:
            conn = self._conn_sync()
            rows = conn.execute(
                "SELECT id, embedding FROM facts "
                "WHERE superseded_by IS NULL AND embedding IS NOT NULL"
            ).fetchall()
            return [(int(row["id"]), bytes(row["embedding"])) for row in rows]

        return await self._run(_load)

    async def keyword_search(self, query: str, *, limit: int) -> Sequence[int]:
        match = _fts_match(query)
        if match is None:
            return []

        def _search() -> list[int]:
            conn = self._conn_sync()
            # facts_fts.rowid == facts.id (content_rowid='id', §8.3), so the join filters to live
            # facts; bm25(facts_fts) is smaller for better matches, hence plain ascending order.
            rows = conn.execute(
                "SELECT f.id FROM facts_fts "
                "JOIN facts f ON f.id = facts_fts.rowid "
                "WHERE facts_fts MATCH ? AND f.superseded_by IS NULL "
                "ORDER BY bm25(facts_fts) LIMIT ?",
                (match, limit),
            ).fetchall()
            return [int(row["id"]) for row in rows]

        return await self._run(_search)

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


class FakeFactRepository(SqliteFactRepo):
    """In-memory :class:`~avid.core.ports.FactRepository` — the P6 fake (SDS §3.9.2).

    Same class, same SQL, same migrations, at ``":memory:"`` — so its cascade and CHECK
    behaviours are the database's, not a re-implementation that could drift. The single writer
    thread holds the one in-memory connection open for the object's lifetime, so the data
    survives across calls (an in-memory DB lives exactly as long as its connection).
    """

    def __init__(self, *, clock: Clock) -> None:
        super().__init__(db_path=":memory:", clock=clock)


__all__ = ["FakeFactRepository", "SqliteFactRepo"]
