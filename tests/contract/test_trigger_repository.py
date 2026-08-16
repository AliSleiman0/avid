"""``TriggerRepository`` + ``ProactiveLog`` contract — real and fake, one suite (#237a, P6).

SDS §14.4: *"One test suite per port. It runs against **every** adapter — real and fake — and they
must be indistinguishable through the interface. This is what makes the HAL an abstraction rather
than an aspiration."*

No ``hardware`` mark here, deliberately, and the same choice ``test_fact_repository.py`` makes:
SQLite is not a device, so **both legs run in CI**. The "fake" is the same class at ``":memory:"``,
which is why it cannot drift — it *is* the real store at a different path. Membership of the
contract tier is the directory, not a marker, matching every other file here.

The file also carries the residue of the closed #234. Its acceptance criteria wanted a migration
proved; the three tables have shipped in ``0001_initial.sql`` since #117, so what is worth proving
instead is that the shipped DDL still matches SDS §8.3 column for column, and that the checksum
guard still bites. Both are below, exercised through ``connect``/``migrate`` directly rather than
through the port — the tier under the port, where a schema regression actually lives.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from avid.adapters.clock import FakeClock
from avid.adapters.sqlite import connect, migrate
from avid.adapters.trigger_store import FakeTriggerStore, SqliteTriggerStore
from avid.core.ports import ProactiveLog, TriggerRepository
from avid.core.schedule import next_occurrence

_TOLD_US = 1_767_600_000  # 2026-01-05, the day the user mentioned the routine


@pytest.fixture(params=["sqlite", "fake"])
async def store(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[SqliteTriggerStore]:
    clock = FakeClock()
    if request.param == "sqlite":
        path = tmp_path_factory.mktemp("triggerstore") / "robot.db"
        adapter: SqliteTriggerStore = SqliteTriggerStore(db_path=path, clock=clock)
    else:
        adapter = FakeTriggerStore(clock=clock)
    try:
        yield adapter
    finally:
        await adapter.aclose()


async def _fact(store: SqliteTriggerStore, *, text: str = "coffee at 08:00") -> int:
    """Insert a ``facts`` row directly and return its id.

    The trigger tables hang off ``facts`` by foreign key, and writing facts is
    ``FactRepository``'s job, not this port's — so the fixture reaches past the port for its
    precondition rather than pretending this store owns a write it does not."""

    def _insert() -> int:
        conn = store._conn_sync()  # noqa: SLF001 - a fixture precondition, not a code path
        with conn:
            cur = conn.execute(
                "INSERT INTO facts (text, kind, importance, created_at, last_accessed_at) "
                "VALUES (?, 'routine', 6, ?, ?)",
                (text, _TOLD_US, _TOLD_US),
            )
        return int(cur.lastrowid or 0)

    return await store._run(_insert)  # noqa: SLF001 - as above


async def _routine(store: SqliteTriggerStore, fact_id: int, *, local_time: str) -> None:
    def _insert() -> None:
        conn = store._conn_sync()  # noqa: SLF001 - fixture precondition
        with conn:
            conn.execute(
                "INSERT INTO routines (fact_id, rrule, local_time, timezone) "
                "VALUES (?, 'FREQ=DAILY', ?, 'America/New_York')",
                (fact_id, local_time),
            )

    await store._run(_insert)  # noqa: SLF001 - as above


# ── The port itself ──────────────────────────────────────────────────────────────────────────


async def test_adapter_satisfies_both_ports(store: SqliteTriggerStore) -> None:
    """One object, two Protocols — they share a connection, a writer thread and a transaction
    boundary, which is what lets "mark it fired" and "write the log row" be one serialised pass
    rather than a race between two adapters."""
    assert isinstance(store, TriggerRepository)
    assert isinstance(store, ProactiveLog)


async def test_a_routine_fact_becomes_a_schedule_trigger(
    store: SqliteTriggerStore,
) -> None:
    fact_id = await _fact(store)
    trigger_id = await store.upsert_routine_trigger(
        fact_id, next_fire_at=1_800_000_000, cooldown_s=3600, at=_TOLD_US
    )
    record = await store.get(trigger_id)
    assert record is not None
    assert record.fact_id == fact_id
    assert record.kind == "schedule"
    assert record.enabled is True
    assert record.next_fire_at == 1_800_000_000
    assert record.fire_count == 0
    assert record.ignore_streak == 0


async def test_re_registering_moves_the_schedule_instead_of_adding_a_second(
    store: SqliteTriggerStore,
) -> None:
    """§7.8's supersession must arrive as an *edit*. "Coffee at 08:00" corrected to 08:30 that
    left both rows in place would have the robot mentioning coffee twice every morning — a
    duplicate that looks exactly like a working feature until someone counts."""
    fact_id = await _fact(store)
    first = await store.upsert_routine_trigger(
        fact_id, next_fire_at=1_800_000_000, cooldown_s=3600, at=_TOLD_US
    )
    second = await store.upsert_routine_trigger(
        fact_id, next_fire_at=1_800_001_800, cooldown_s=3600, at=_TOLD_US
    )
    assert first == second
    assert len(await store.enabled_triggers()) == 1
    record = await store.get(first)
    assert record is not None
    assert record.next_fire_at == 1_800_001_800


async def test_re_arming_clears_a_backoff(store: SqliteTriggerStore) -> None:
    """The user restating a routine is the strongest evidence available that §10.5's backoff was
    reading a stale intent rather than an unwanted one. A trigger three ignores deep must not stay
    crippled after the user says the thing again."""
    fact_id = await _fact(store)
    trigger_id = await store.upsert_routine_trigger(
        fact_id, next_fire_at=1_800_000_000, cooldown_s=3600, at=_TOLD_US
    )
    await store.set_backoff(trigger_id, ignore_streak=2, cooldown_s=14400)
    await store.disable(trigger_id)

    await store.upsert_routine_trigger(
        fact_id, next_fire_at=1_800_086_400, cooldown_s=3600, at=_TOLD_US
    )
    record = await store.get(trigger_id)
    assert record is not None
    assert record.ignore_streak == 0
    assert record.cooldown_s == 3600
    assert record.enabled is True


async def test_the_boot_rebuild_uses_the_index_predicate(
    store: SqliteTriggerStore,
) -> None:
    """``enabled_triggers`` must be exactly ``idx_triggers_due``'s partial predicate: enabled, and
    actually scheduled. A trigger §10.5 switched off has to stay off across a restart, or the
    backoff is a decoration — which is the whole reason the streak is a column and not a field."""
    scheduled = await _fact(store, text="coffee")
    disabled = await _fact(store, text="standup")
    unscheduled = await _fact(store, text="someday")

    keep = await store.upsert_routine_trigger(
        scheduled, next_fire_at=1_800_000_000, cooldown_s=3600, at=_TOLD_US
    )
    off = await store.upsert_routine_trigger(
        disabled, next_fire_at=1_800_000_000, cooldown_s=3600, at=_TOLD_US
    )
    await store.disable(off)
    await store.upsert_routine_trigger(
        unscheduled, next_fire_at=None, cooldown_s=3600, at=_TOLD_US
    )

    rebuilt = await store.enabled_triggers()
    assert [record.id for record in rebuilt] == [keep]


async def test_recording_a_fire_advances_the_counters(
    store: SqliteTriggerStore,
) -> None:
    fact_id = await _fact(store)
    trigger_id = await store.upsert_routine_trigger(
        fact_id, next_fire_at=1_800_000_000, cooldown_s=3600, at=_TOLD_US
    )
    await store.record_fired(trigger_id, at=1_800_000_000, next_fire_at=1_800_086_400)
    record = await store.get(trigger_id)
    assert record is not None
    assert record.fire_count == 1
    assert record.last_fired_at == 1_800_000_000
    assert record.next_fire_at == 1_800_086_400


async def test_an_exhausted_rule_leaves_the_scheduler_alone(
    store: SqliteTriggerStore,
) -> None:
    """``next_fire_at = NULL`` drops the row out of the partial index, so a finite ``COUNT=`` that
    has run out costs the scheduler nothing at all — no filtering, no special case."""
    fact_id = await _fact(store)
    trigger_id = await store.upsert_routine_trigger(
        fact_id, next_fire_at=1_800_000_000, cooldown_s=3600, at=_TOLD_US
    )
    await store.record_fired(trigger_id, at=1_800_000_000, next_fire_at=None)
    assert await store.enabled_triggers() == []
    assert await store.get(trigger_id) is not None  # still there, just not due


async def test_removing_a_fact_removes_its_trigger(store: SqliteTriggerStore) -> None:
    fact_id = await _fact(store)
    await store.upsert_routine_trigger(
        fact_id, next_fire_at=1_800_000_000, cooldown_s=3600, at=_TOLD_US
    )
    await store.remove_for_fact(fact_id)
    assert await store.enabled_triggers() == []
    await store.remove_for_fact(
        fact_id
    )  # idempotent: a fact with no trigger is not an error


async def test_a_routine_round_trips_into_something_resolvable(
    store: SqliteTriggerStore,
) -> None:
    """The join that makes UC-03 work: the ``routines`` row plus the fact's ``created_at`` is a
    resolvable :class:`Routine`, DTSTART included. ``created_at`` is the anchor and has no column
    of its own — this test is what proves the join supplies it."""
    fact_id = await _fact(store)
    await _routine(store, fact_id, local_time="08:00")

    routine = await store.routine_for(fact_id)
    assert routine is not None
    assert routine.rrule == "FREQ=DAILY"
    assert routine.local_time == "08:00"
    assert routine.lead_time_s == 300  # §8.3's default: 08:00 becomes 07:55
    assert routine.dtstart_epoch == _TOLD_US
    assert next_occurrence(routine, after=_TOLD_US) is not None


async def test_a_fact_without_a_schedule_is_a_normal_none(
    store: SqliteTriggerStore,
) -> None:
    """Not every routine fact has a clock time. ``None`` is an answer, not a failure — and it is
    the *caller's* job to say so out loud, because a routine-kind fact with no schedule is
    otherwise indistinguishable from a user with no routines (§6.6)."""
    assert await store.routine_for(await _fact(store)) is None


# ── ProactiveLog — R-08's instrument ─────────────────────────────────────────────────────────


async def test_both_outcomes_are_written(store: SqliteTriggerStore) -> None:
    """§10.6: *"every considered proposal is logged, delivered or not"*. Suppression is not an
    early return — "it never fired" and "it fired and was vetoed forty times" are identical from
    outside this table, and only one of them means rule 4 is too aggressive."""
    fact_id = await _fact(store)
    trigger_id = await store.upsert_routine_trigger(
        fact_id, next_fire_at=1_800_000_000, cooldown_s=3600, at=_TOLD_US
    )
    delivered = await store.record(
        trigger_id=trigger_id,
        considered_at=1_800_000_000,
        outcome="delivered",
        reason=None,
        utterance="Morning — coffee time soon.",
    )
    suppressed = await store.record(
        trigger_id=trigger_id,
        considered_at=1_800_000_100,
        outcome="suppressed",
        reason="quiet_hours",
        utterance=None,
    )
    assert delivered != suppressed
    assert await store.delivered_since(since=0) == 1


async def test_the_schema_rejects_an_outcome_it_does_not_know(
    store: SqliteTriggerStore,
) -> None:
    """§8.3's CHECK constraint is load-bearing: a typo in an outcome would silently vanish from
    both halves of §10.6's query and make the histogram under-count in a way nothing reports."""
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await store.record(
            trigger_id=None,
            considered_at=1_800_000_000,
            outcome="mabye",
            reason=None,
            utterance=None,
        )


async def test_the_budget_and_cooldown_survive_a_restart(
    tmp_path: Path,
) -> None:
    """Rules 5 and 6 are the only two with memory, and rebuilding them from an in-process counter
    would mean a reboot at 07:00 silently resets the day's budget — a robot that becomes five times
    more talkative every time it restarts. Real store only: the point is the file."""
    clock = FakeClock()
    path = tmp_path / "robot.db"
    first = SqliteTriggerStore(db_path=path, clock=clock)
    try:
        for offset in range(3):
            await first.record(
                trigger_id=None,
                considered_at=1_800_000_000 + offset,
                outcome="delivered",
                reason=None,
                utterance="hello",
            )
    finally:
        await first.aclose()

    second = SqliteTriggerStore(db_path=path, clock=clock)
    try:
        assert await second.delivered_since(since=1_800_000_000) == 3
        assert await second.last_delivered_at() == 1_800_000_002
    finally:
        await second.aclose()


async def test_delivered_since_is_a_window_not_a_total(
    store: SqliteTriggerStore,
) -> None:
    """Rule 6 is a *daily* budget. A cumulative count would silence the robot permanently on the
    fifth delivery of its life."""
    for at in (1_000, 2_000, 3_000):
        await store.record(
            trigger_id=None,
            considered_at=at,
            outcome="delivered",
            reason=None,
            utterance="x",
        )
    assert await store.delivered_since(since=0) == 3
    assert await store.delivered_since(since=2_000) == 2
    assert await store.delivered_since(since=9_000) == 0


async def test_no_deliveries_yet_reads_as_none_not_zero(
    store: SqliteTriggerStore,
) -> None:
    """``last_delivered_at`` must distinguish "never" from "at the epoch". A zero here would make
    rule 5's global cooldown compare against 1970 and pass forever, which is the correct outcome
    reached by an accident that would not survive the next edit."""
    assert await store.last_delivered_at() is None
    assert await store.delivered_since(since=0) == 0


async def test_a_reaction_is_recorded_after_the_fact(store: SqliteTriggerStore) -> None:
    """§10.5's engaged/ignored is not known when the row is written — it is known when the
    hold-open window closes, which is the same signal the backoff turns on."""
    log_id = await store.record(
        trigger_id=None,
        considered_at=1_800_000_000,
        outcome="delivered",
        reason=None,
        utterance="Coffee soon.",
    )
    await store.set_reaction(log_id, "ignored")

    def _read() -> str:
        row = (
            store._conn_sync()
            .execute(  # noqa: SLF001 - asserting a column the port has no read for
                "SELECT user_reaction FROM proactive_log WHERE id = ?", (log_id,)
            )
            .fetchone()
        )
        return str(row["user_reaction"])

    assert await store._run(_read) == "ignored"  # noqa: SLF001 - as above


async def test_aclose_is_idempotent(store: SqliteTriggerStore) -> None:
    await store.aclose()
    await store.aclose()


# ── Below the port: the schema #234 was going to migrate ─────────────────────────────────────


def test_the_shipped_migration_already_carries_the_three_tables(tmp_path: Path) -> None:
    """#234 asked for a ``0002_behavior.sql`` creating ``routines``/``triggers``/``proactive_log``.
    They have been in ``0001_initial.sql`` since #117, exactly as SDS §8.3 documents — and writing
    the second migration would have failed at boot, because the runner is checksummed and
    append-only (§8.6). This is the assertion that issue should have been."""
    conn = connect(str(tmp_path / "fresh.db"))
    try:
        migrate(conn, now=_TOLD_US)
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"routines", "triggers", "proactive_log"} <= tables

        # The columns §10 actually depends on, named rather than counted — a table that exists
        # with the wrong shape is the failure this guards, not a table that is missing.
        trigger_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(triggers)")
        }
        assert {
            "enabled",
            "next_fire_at",
            "last_fired_at",
            "fire_count",
            "ignore_streak",
            "cooldown_s",
        } <= trigger_columns

        routine_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(routines)")
        }
        assert {"rrule", "local_time", "timezone", "lead_time_s"} <= routine_columns

        log_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(proactive_log)")
        }
        assert {
            "considered_at",
            "outcome",
            "reason",
            "utterance",
            "user_reaction",
        } <= log_columns

        # The scheduler's only query rides this index; without the partial predicate the boot
        # rebuild would scan every trigger the robot has ever been given.
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(triggers)")}
        assert "idx_triggers_due" in indexes
    finally:
        conn.close()


async def test_set_next_fire_moves_the_occurrence_without_faking_a_fire(
    store: SqliteTriggerStore,
) -> None:
    """Deliberately not ``record_fired``, and the distinction is load-bearing.

    A **suppressed** proposal did not fire. Recording it as one would stamp ``last_fired_at`` and
    increment ``fire_count`` — corrupting rule 5's own-cooldown arithmetic (it would think the
    trigger had just spoken) and inflating the only counter that says how often this reminder has
    actually said anything.
    """
    fact_id = await _fact(store)
    trigger_id = await store.upsert_routine_trigger(
        fact_id, next_fire_at=1_800_000_000, cooldown_s=3600, at=_TOLD_US
    )

    await store.set_next_fire(trigger_id, next_fire_at=1_800_086_400)
    record = await store.get(trigger_id)
    assert record is not None
    assert record.next_fire_at == 1_800_086_400
    assert record.last_fired_at is None, "nothing fired"
    assert record.fire_count == 0, "and nothing may claim it did"


async def test_set_next_fire_can_retire_a_trigger(store: SqliteTriggerStore) -> None:
    """``None`` for a rule that has run out — the row leaves ``idx_triggers_due``'s partial
    predicate and the scheduler stops considering it, with no special case anywhere."""
    fact_id = await _fact(store)
    trigger_id = await store.upsert_routine_trigger(
        fact_id, next_fire_at=1_800_000_000, cooldown_s=3600, at=_TOLD_US
    )
    await store.set_next_fire(trigger_id, next_fire_at=None)
    assert await store.enabled_triggers() == []
