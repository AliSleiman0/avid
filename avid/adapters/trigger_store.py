"""``TriggerRepository`` + ``ProactiveLog`` adapters — the behaviour engine's persistence (#237a).

The ``triggers``, ``routines`` and ``proactive_log`` tables behind their two ports (SDS §8.3, §10.3,
§10.6). One class satisfies both Protocols: they share a connection, a writer thread and a
transaction boundary, and the two things a proactive decision does — mark the trigger fired, write
the log row — want to be one call on one thread rather than a race between two adapters.

Mirrors :mod:`avid.adapters.episode_store` exactly, and deliberately: the same
:mod:`avid.adapters.sqlite` machinery, the same **single writer thread** (P8, §3.8.2) keeping
blocking ``sqlite3`` off the loop *and* serialising every call, and the same real/fake split where
the fake is the same class at ``":memory:"``. A fake that re-implemented any of this could drift
from the real store; this one cannot, because it *is* the real store at a different path (P6).

⚠️ **No migration ships with this module.** All three tables have been in
``migrations/0001_initial.sql`` since #117 (lines 46-89), exactly as SDS §8.3 documents. The
milestone's original plan was a ``0002_behavior.sql``; it would have failed at boot, because the
runner is checksummed and append-only (§8.6) and a second ``CREATE TABLE routines`` cannot apply.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sqlite3 import Connection, Row
from typing import TypeVar

from avid.adapters.sqlite import connect, migrate
from avid.core.ports import Clock
from avid.core.schedule import Routine
from avid.domain.behavior import TriggerRecord

_log = logging.getLogger("avid.adapters.trigger_store")

_T = TypeVar("_T")

# Named once so the SELECTs and the row mapper cannot drift apart — the same guard
# `_FACT_COLUMNS` gives `fact_repository`.
_TRIGGER_COLUMNS = (
    "id, fact_id, kind, enabled, next_fire_at, last_fired_at, "
    "fire_count, ignore_streak, cooldown_s"
)

# Exactly `idx_triggers_due`'s partial predicate (§8.3). Written here as one constant so the boot
# rebuild uses the index rather than merely resembling it.
_DUE_PREDICATE = "enabled = 1 AND next_fire_at IS NOT NULL"


def _to_trigger(row: Row) -> TriggerRecord:
    """One ``triggers`` row → the domain value. ``enabled`` crosses as a bool; SQLite stores 0/1."""
    return TriggerRecord(
        id=row["id"],
        fact_id=row["fact_id"],
        kind=row["kind"],
        enabled=bool(row["enabled"]),
        next_fire_at=row["next_fire_at"],
        last_fired_at=row["last_fired_at"],
        fire_count=row["fire_count"],
        ignore_streak=row["ignore_streak"],
        cooldown_s=row["cooldown_s"],
    )


class SqliteTriggerStore:
    """File-backed :class:`~avid.core.ports.TriggerRepository` and
    :class:`~avid.core.ports.ProactiveLog`, off-loop on one writer thread."""

    def __init__(self, *, db_path: str | Path, clock: Clock) -> None:
        self._db_path = str(db_path)
        self._clock = clock
        # max_workers=1: §3.8.2's single writer. It serialises every call and owns the one
        # Connection, satisfying sqlite3's thread-affinity — and making upsert-if-absent race-free
        # without a UNIQUE constraint, which `triggers` does not have on fact_id.
        self._pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="trigger-store"
        )
        self._conn: Connection | None = None  # opened lazily, on the worker thread
        self._closed = False

    # -- worker-thread helpers (never touched from the event loop) --------------

    def _conn_sync(self) -> Connection:
        """The connection, opened + migrated on first use — on the writer thread, so the
        ``Connection`` is created on the one thread that will ever touch it. ``migrate`` is
        idempotent (§8.6), so it no-ops when another store already migrated the shared file."""
        if self._conn is None:
            _log.debug("opening trigger store at %s", self._db_path)
            conn = connect(self._db_path)
            migrate(conn, now=self._clock.now())
            self._conn = conn
        return self._conn

    async def _run(self, fn: Callable[[], _T]) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, fn)

    # -- TriggerRepository ---------------------------------------------------------

    async def upsert_routine_trigger(
        self, fact_id: int, *, next_fire_at: int | None, cooldown_s: int, at: int
    ) -> int:
        def _upsert() -> int:
            conn = self._conn_sync()
            with conn:
                existing = conn.execute(
                    "SELECT id FROM triggers WHERE fact_id = ? AND kind = 'schedule'",
                    (fact_id,),
                ).fetchone()
                if existing is not None:
                    # Re-arming resets the backoff. The user has just restated the routine, which
                    # is the strongest evidence available that §10.5 was reading a stale intent
                    # rather than an unwanted one — see the port docstring.
                    conn.execute(
                        "UPDATE triggers SET enabled = 1, next_fire_at = ?, "
                        "ignore_streak = 0, cooldown_s = ? WHERE id = ?",
                        (next_fire_at, cooldown_s, existing["id"]),
                    )
                    return int(existing["id"])
                cur = conn.execute(
                    "INSERT INTO triggers (fact_id, kind, enabled, next_fire_at, cooldown_s) "
                    "VALUES (?, 'schedule', 1, ?, ?)",
                    (fact_id, next_fire_at, cooldown_s),
                )
            return int(cur.lastrowid or 0)

        return await self._run(_upsert)

    async def remove_for_fact(self, fact_id: int) -> None:
        def _remove() -> None:
            conn = self._conn_sync()
            with conn:
                conn.execute("DELETE FROM triggers WHERE fact_id = ?", (fact_id,))

        await self._run(_remove)

    async def enabled_triggers(self) -> Sequence[TriggerRecord]:
        def _enabled() -> Sequence[TriggerRecord]:
            conn = self._conn_sync()
            rows = conn.execute(
                f"SELECT {_TRIGGER_COLUMNS} FROM triggers WHERE {_DUE_PREDICATE} "  # noqa: S608 - a module constant, no user input reaches this string
                "ORDER BY next_fire_at"
            ).fetchall()
            return [_to_trigger(row) for row in rows]

        return await self._run(_enabled)

    async def get(self, trigger_id: int) -> TriggerRecord | None:
        def _get() -> TriggerRecord | None:
            conn = self._conn_sync()
            row = conn.execute(
                f"SELECT {_TRIGGER_COLUMNS} FROM triggers WHERE id = ?",  # noqa: S608 - module constant
                (trigger_id,),
            ).fetchone()
            return None if row is None else _to_trigger(row)

        return await self._run(_get)

    async def routine_for(self, fact_id: int) -> Routine | None:
        def _routine() -> Routine | None:
            conn = self._conn_sync()
            row = conn.execute(
                "SELECT r.rrule, r.local_time, r.timezone, r.lead_time_s, f.created_at "
                "FROM routines r JOIN facts f ON f.id = r.fact_id WHERE r.fact_id = ?",
                (fact_id,),
            ).fetchone()
            if row is None:
                return None
            # DTSTART is the fact's own created_at — the day the user told us about the routine.
            # It has no column of its own because it does not need one, and anchoring it anywhere
            # else changes what the rule means (see core/schedule.py).
            return Routine(
                rrule=row["rrule"],
                local_time=row["local_time"],
                timezone=row["timezone"],
                dtstart_epoch=row["created_at"],
                lead_time_s=row["lead_time_s"],
            )

        return await self._run(_routine)

    async def record_fired(
        self, trigger_id: int, *, at: int, next_fire_at: int | None
    ) -> None:
        def _fired() -> None:
            conn = self._conn_sync()
            with conn:
                conn.execute(
                    "UPDATE triggers SET last_fired_at = ?, fire_count = fire_count + 1, "
                    "next_fire_at = ? WHERE id = ?",
                    (at, next_fire_at, trigger_id),
                )

        await self._run(_fired)

    async def set_next_fire(self, trigger_id: int, *, next_fire_at: int | None) -> None:
        def _set() -> None:
            conn = self._conn_sync()
            with conn:
                conn.execute(
                    "UPDATE triggers SET next_fire_at = ? WHERE id = ?",
                    (next_fire_at, trigger_id),
                )

        await self._run(_set)

    async def set_backoff(
        self, trigger_id: int, *, ignore_streak: int, cooldown_s: int
    ) -> None:
        def _backoff() -> None:
            conn = self._conn_sync()
            with conn:
                conn.execute(
                    "UPDATE triggers SET ignore_streak = ?, cooldown_s = ? WHERE id = ?",
                    (ignore_streak, cooldown_s, trigger_id),
                )

        await self._run(_backoff)

    async def disable(self, trigger_id: int) -> None:
        def _disable() -> None:
            conn = self._conn_sync()
            with conn:
                conn.execute(
                    "UPDATE triggers SET enabled = 0 WHERE id = ?", (trigger_id,)
                )

        await self._run(_disable)

    # -- ProactiveLog --------------------------------------------------------------

    async def record(
        self,
        *,
        trigger_id: int | None,
        considered_at: int,
        outcome: str,
        reason: str | None,
        utterance: str | None,
    ) -> int:
        def _record() -> int:
            conn = self._conn_sync()
            with conn:
                cur = conn.execute(
                    "INSERT INTO proactive_log "
                    "(trigger_id, considered_at, outcome, reason, utterance) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (trigger_id, considered_at, outcome, reason, utterance),
                )
            return int(cur.lastrowid or 0)

        return await self._run(_record)

    async def set_reaction(self, log_id: int, reaction: str) -> None:
        def _reaction() -> None:
            conn = self._conn_sync()
            with conn:
                conn.execute(
                    "UPDATE proactive_log SET user_reaction = ? WHERE id = ?",
                    (reaction, log_id),
                )

        await self._run(_reaction)

    async def delivered_since(self, *, since: int) -> int:
        def _count() -> int:
            conn = self._conn_sync()
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM proactive_log "
                "WHERE outcome = 'delivered' AND considered_at >= ?",
                (since,),
            ).fetchone()
            return int(row["n"])

        return await self._run(_count)

    async def last_delivered_at(self) -> int | None:
        def _last() -> int | None:
            conn = self._conn_sync()
            row = conn.execute(
                "SELECT MAX(considered_at) AS at FROM proactive_log "
                "WHERE outcome = 'delivered'"
            ).fetchone()
            return None if row["at"] is None else int(row["at"])

        return await self._run(_last)

    # -- lifecycle -----------------------------------------------------------------

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


class FakeTriggerStore(SqliteTriggerStore):
    """In-memory trigger store — the P6 fake (SDS §3.9.2).

    Same class, same SQL, same migrations, at ``":memory:"``. The single writer thread holds the
    one in-memory connection open for the object's lifetime, so data survives across calls — and
    dies with the object, which is what makes a restart test meaningful when the *real* store is
    pointed at a file.
    """

    def __init__(self, *, clock: Clock) -> None:
        super().__init__(db_path=":memory:", clock=clock)


__all__ = ["FakeTriggerStore", "SqliteTriggerStore"]
