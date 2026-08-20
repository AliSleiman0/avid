"""Contract tests for :class:`~avid.core.ports.BootLog` (#379, P6).

Run against ``FakeBootLog``, which is ``SqliteBootLog`` at ``":memory:"`` — same class, same SQL,
same migrations. That matters more here than for most fakes: the two behaviours worth testing are
the ``CHECK`` that ``stopped_at`` and ``stop_reason`` agree and the heartbeat's
``stopped_at IS NULL`` guard, and **both belong to the database**. A hand-written fake would be
testing a re-implementation instead of the thing that ships.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from avid.adapters import FakeBootLog, SqliteBootLog
from avid.adapters.clock import FakeClock
from avid.core.ports import BootLog


@pytest.fixture
async def log() -> AsyncIterator[tuple[FakeBootLog, FakeClock]]:
    clock = FakeClock()
    store = FakeBootLog(clock=clock)
    try:
        yield store, clock
    finally:
        await store.aclose()


def test_the_fake_satisfies_the_port_structurally() -> None:
    assert isinstance(FakeBootLog(clock=FakeClock()), BootLog)


async def test_an_open_run_has_no_stop(log: tuple[FakeBootLog, FakeClock]) -> None:
    store, clock = log
    await store.open_boot(boot_id="b1", build="1.2.3")
    (record,) = await store.records(since=0, until=10**12)
    assert record.boot_id == "b1"
    assert record.build == "1.2.3"
    assert record.stopped_at is None
    assert record.was_clean is False  # an open row IS the unplanned-stop representation


async def test_a_heartbeat_moves_last_seen(log: tuple[FakeBootLog, FakeClock]) -> None:
    store, clock = log
    await store.open_boot(boot_id="b1", build="x")
    started = (await store.records(since=0, until=10**12))[0].last_seen_at
    await clock.advance(120)
    await store.heartbeat()
    (record,) = await store.records(since=0, until=10**12)
    assert record.last_seen_at == started + 120
    assert record.stopped_at is None  # a heartbeat is not a stop


async def test_a_clean_stop_records_both_halves(
    log: tuple[FakeBootLog, FakeClock],
) -> None:
    store, clock = log
    await store.open_boot(boot_id="b1", build="x")
    await clock.advance(60)
    await store.close_boot(reason="signal")
    (record,) = await store.records(since=0, until=10**12)
    assert record.was_clean is True
    assert record.stop_reason == "signal"
    assert record.stopped_at == record.last_seen_at


async def test_a_late_heartbeat_cannot_resurrect_a_stopped_run(
    log: tuple[FakeBootLog, FakeClock],
) -> None:
    """The one race this adapter has, and the assertion that guards it.

    A heartbeat scheduled before the teardown but running after it would otherwise push
    ``last_seen_at`` past the stop — making a run that both ended cleanly and went on living,
    which is not a fact. The lifecycle cancels the beat first; the SQL guards it anyway, because
    the two are written years apart by definition.
    """
    store, clock = log
    await store.open_boot(boot_id="b1", build="x")
    await store.close_boot(reason="signal")
    stopped = (await store.records(since=0, until=10**12))[0]
    await clock.advance(600)
    await store.heartbeat()
    (record,) = await store.records(since=0, until=10**12)
    assert record.last_seen_at == stopped.last_seen_at
    assert record.stopped_at == stopped.stopped_at


async def test_records_returns_runs_overlapping_the_window_oldest_first(
    log: tuple[FakeBootLog, FakeClock],
) -> None:
    """Overlap, not containment — the run already going when the window opened carries its first
    seconds, and dropping it understates availability at both ends of every window."""
    store, clock = log
    await store.open_boot(boot_id="old", build="x")
    await clock.advance(100)
    await store.close_boot(reason="signal")
    start_of_second = clock.now()
    await clock.advance(50)
    await store.open_boot(boot_id="new", build="x")
    await clock.advance(50)
    await store.heartbeat()

    both = await store.records(since=0, until=10**12)
    assert [r.boot_id for r in both] == ["old", "new"]

    # A window opening inside the gap sees only the later run.
    later = await store.records(since=start_of_second + 10, until=10**12)
    assert [r.boot_id for r in later] == ["new"]


async def test_records_is_empty_outside_every_run(
    log: tuple[FakeBootLog, FakeClock],
) -> None:
    store, clock = log
    await store.open_boot(boot_id="b1", build="x")
    await store.close_boot(reason="signal")
    assert await store.records(since=10**11, until=10**12) == []


async def test_heartbeat_and_close_before_any_boot_are_no_ops(
    log: tuple[FakeBootLog, FakeClock],
) -> None:
    """An instrument must not raise on a path the robot might legitimately take (§3.12.3)."""
    store, _ = log
    await store.heartbeat()
    await store.close_boot(reason="signal")
    assert await store.records(since=0, until=10**12) == []


async def test_the_real_adapter_persists_across_instances(tmp_path: object) -> None:
    """The property the in-memory fake cannot have, so it is asserted against the file-backed one:
    a restart must be able to read the previous run. Uptime across a restart is the entire point.
    """
    from pathlib import Path

    db = Path(str(tmp_path)) / "robot.db"
    clock = FakeClock()
    first = SqliteBootLog(db_path=db, clock=clock)
    await first.open_boot(boot_id="b1", build="x")
    await clock.advance(30)
    await first.close_boot(reason="signal")
    await first.aclose()

    await clock.advance(10)
    second = SqliteBootLog(db_path=db, clock=clock)
    await second.open_boot(boot_id="b2", build="x")
    records = await second.records(since=0, until=10**12)
    await second.aclose()
    assert [r.boot_id for r in records] == ["b1", "b2"]


async def test_aclose_is_idempotent(log: tuple[FakeBootLog, FakeClock]) -> None:
    store, _ = log
    await store.aclose()
    await store.aclose()
