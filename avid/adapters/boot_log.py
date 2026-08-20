"""The :class:`~avid.core.ports.BootLog` over SQLite (#379, SDS §12.6).

Same shape as ``SqliteTriggerStore``: one writer thread owning one ``Connection``, every call
off-loop (P8), migrations applied idempotently on first use. It writes to the robot's one database
because ``proactive_log`` is the precedent — an operational audit table read afterwards with plain
SQL, which is how the M11 gate will read this.

**Write volume is the design constraint.** This is the only thing in the system that writes on a
timer with nothing happening, so it writes as little as possible: one ``INSERT`` per boot, one
one-column ``UPDATE`` per heartbeat (60 s by default — ~1,440 a day), one ``UPDATE`` per clean
stop. §2.7.1's storage row is *"Binding. Random write is slow and the card wears out"*, and a
reliability feature that wore out the card would be a poor joke.

⚠️ **Nothing here ever writes "crashed", because nothing can.** A crash, a watchdog kill and a
power cut all skip this module entirely. The representation of an unplanned stop is a row whose
``stopped_at`` is still NULL — an absence, not a record — and that is exactly why the heartbeat
exists: without it, the last *known* liveness of a crashed run would be the boot itself.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sqlite3 import Connection, Row
from typing import TypeVar, cast

from avid.adapters.sqlite import connect, migrate
from avid.core.ports import Clock
from avid.domain import BootRecord, StopReason

_log = logging.getLogger("avid.adapters.boot_log")

_T = TypeVar("_T")


def _row_to_record(row: Row) -> BootRecord:
    return BootRecord(
        boot_id=str(row["boot_id"]),
        build=str(row["build"]),
        started_at=int(row["started_at"]),
        started_mono=int(row["started_mono"]),
        last_seen_at=int(row["last_seen_at"]),
        stopped_at=None if row["stopped_at"] is None else int(row["stopped_at"]),
        stop_reason=(
            None
            if row["stop_reason"] is None
            else cast(StopReason, str(row["stop_reason"]))
        ),
    )


class SqliteBootLog:
    """File-backed :class:`~avid.core.ports.BootLog`, off-loop on one writer thread."""

    def __init__(self, *, db_path: str | Path, clock: Clock) -> None:
        self._db_path = str(db_path)
        self._clock = clock
        # max_workers=1: §3.8.2's single writer, and it owns the one Connection so sqlite3's
        # thread-affinity holds. It also serialises heartbeat-against-close, which is the only
        # race this adapter has — a heartbeat landing after the stop would reopen a closed run.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="boot-log")
        self._conn: Connection | None = None  # opened lazily, on the worker thread
        self._row_id: int | None = None
        self._closed = False

    def _conn_sync(self) -> Connection:
        if self._conn is None:
            _log.debug("opening boot log at %s", self._db_path)
            conn = connect(self._db_path)
            migrate(conn, now=self._clock.now())
            self._conn = conn
        return self._conn

    async def _run(self, fn: Callable[[], _T]) -> _T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._pool, fn)

    async def open_boot(self, *, boot_id: str, build: str) -> None:
        now = self._clock.now()
        mono = self._clock.monotonic_ns()

        def _open() -> int:
            conn = self._conn_sync()
            with conn:
                cur = conn.execute(
                    "INSERT INTO boot_log (boot_id, build, started_at, started_mono, "
                    "last_seen_at) VALUES (?,?,?,?,?)",
                    (boot_id, build, now, mono, now),
                )
            return int(cast(int, cur.lastrowid))

        self._row_id = await self._run(_open)

    async def heartbeat(self) -> None:
        """One column, one row. Deliberately not a no-op when the clock has not moved: the cost is
        a single page write and the value is that "still alive" is asserted rather than inferred."""
        if self._row_id is None:
            return
        now = self._clock.now()
        row_id = self._row_id

        def _beat() -> None:
            conn = self._conn_sync()
            with conn:
                # `stopped_at IS NULL` guards the one race worth guarding: a heartbeat scheduled
                # before the teardown but running after it would otherwise resurrect a closed run,
                # and a run that both stopped cleanly and kept beating is not a fact.
                conn.execute(
                    "UPDATE boot_log SET last_seen_at = ? WHERE id = ? AND stopped_at IS NULL",
                    (now, row_id),
                )

        await self._run(_beat)

    async def close_boot(self, *, reason: StopReason) -> None:
        if self._row_id is None:
            return
        now = self._clock.now()
        row_id = self._row_id

        def _close() -> None:
            conn = self._conn_sync()
            with conn:
                conn.execute(
                    "UPDATE boot_log SET stopped_at = ?, stop_reason = ?, last_seen_at = ? "
                    "WHERE id = ?",
                    (now, reason, now, row_id),
                )

        await self._run(_close)

    async def records(self, *, since: int, until: int) -> Sequence[BootRecord]:
        def _read() -> list[BootRecord]:
            conn = self._conn_sync()
            # OVERLAP, not containment: the run already going when a window opened carries that
            # window's first seconds. `COALESCE(stopped_at, last_seen_at)` is the run's known end,
            # matching BootRecord.ended_at — the conservative reading, so an unclean run is never
            # credited with uptime it cannot prove.
            rows = conn.execute(
                "SELECT * FROM boot_log WHERE started_at < ? "
                "AND COALESCE(stopped_at, last_seen_at) > ? ORDER BY started_at",
                (until, since),
            ).fetchall()
            return [_row_to_record(r) for r in rows]

        return await self._run(_read)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True

        def _shut() -> None:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

        await self._run(_shut)
        self._pool.shutdown(wait=True)


class FakeBootLog(SqliteBootLog):
    """In-memory boot log — the P6 fake (SDS §3.9.2).

    Same class, same SQL, same migrations, at ``":memory:"``. Which matters more here than for
    most fakes: the interesting behaviours are the `CHECK` that ``stopped_at`` and ``stop_reason``
    agree, and the heartbeat's ``stopped_at IS NULL`` guard — both are the *database's*, so a
    hand-written fake would be testing a re-implementation rather than the thing that ships.
    """

    def __init__(self, *, clock: Clock) -> None:
        super().__init__(db_path=":memory:", clock=clock)


__all__ = ["FakeBootLog", "SqliteBootLog"]
