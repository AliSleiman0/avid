"""``BehaviorService`` — fire → gate → log, and the world it reads (#237, SDS §10.2, §10.6).

Driven against the P6 fakes and the **real** ``AsyncioEventBus`` and ``StateManager``, no mocks
(SDS §14.3). The service is application code, so these tests are both its behaviour proof and its
coverage.

The one that matters most is ``test_every_proposal_writes_exactly_one_row``. §10.6 is blunt about
why: *"without this table, [under-firing and never-firing] look identical from the outside"* — and
R-08, the highest-scored risk in the register, is precisely the one nobody can see happening. A
suppression that returns early instead of logging costs nothing today and costs the entire tuning
story in month three.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from uuid import uuid4

import pytest

from avid.adapters import FakeTriggerStore
from avid.adapters.clock import FakeClock
from avid.core.envelope import envelope
from avid.core.event_bus import AsyncioEventBus
from avid.core.state_manager import StateManager
from avid.domain import (
    AudioSpeechEnded,
    AudioSpeechStarted,
    BehaviorProactiveDelivered,
    BehaviorProactiveSuppressed,
    BehaviorTriggerDisabled,
    BehaviorTriggerFired,
    ConversationUserTranscribed,
    Event,
    MemoryFactDeleted,
    MemoryFactStored,
    PolicyLimits,
    RobotState,
    SystemStarted,
    Trigger,
    VisionPresenceGained,
)
from avid.services.behavior import BehaviorService, _PendingDelivery

# 2026-06-10 12:00 UTC. Far from any DST edge in the test zone, so a shifted expectation is a real
# failure rather than a calendar accident.
_START = 1_781_438_400
_ZONE = "UTC"

_LIMITS = PolicyLimits(
    quiet_start_minutes=22 * 60,
    quiet_end_minutes=7 * 60 + 30,
    presence_window_s=300,
    ambient_speech_threshold_s=60,
    global_cooldown_s=900,
    daily_budget=5,
)


@dataclass
class Rig:
    behavior: BehaviorService
    store: FakeTriggerStore
    bus: AsyncioEventBus
    clock: FakeClock
    state: StateManager
    events: list[Event]


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    clock = FakeClock(start=_START)
    bus = AsyncioEventBus(clock=clock)
    state = StateManager(bus=bus, clock=clock)
    store = FakeTriggerStore(clock=clock)
    seen: list[Event] = []

    async def _collect(event: Event) -> None:
        seen.append(event)

    behavior = BehaviorService(
        bus=bus,
        clock=clock,
        state=state,
        triggers=store,
        proactive_log=store,
        limits=_LIMITS,
        timezone=_ZONE,
        default_cooldown_s=900,
        hold_open_s=30.0,
        ignore_backoff_multiplier=2,
        ignore_streak_limit=3,
    )
    for sub in behavior.subscriptions():
        bus.subscribe(
            sub.event_type,
            sub.handler,
            name=sub.name,
            policy=sub.policy,
            maxsize=sub.maxsize,
        )
    for event_type in (
        BehaviorTriggerFired,
        BehaviorProactiveDelivered,
        BehaviorProactiveSuppressed,
        BehaviorTriggerDisabled,
    ):
        bus.subscribe(event_type, _collect, name=f"test.{event_type.name}")
    await bus.start()
    await behavior.start()
    try:
        yield Rig(behavior, store, bus, clock, state, seen)
    finally:
        await behavior.stop()
        await bus.stop()
        await store.aclose()


async def _settle(rig: Rig, rounds: int = 4) -> None:
    """Let the bus deliver, and let the store's writer thread actually finish.

    Yielding the loop is not enough here and that is worth stating: every handler in this service
    ends in a ``run_in_executor`` call on the store's **single** writer thread, so a plain
    ``asyncio.sleep(0)`` returns while the DB work is still queued. Submitting a no-op through the
    same one-worker pool and awaiting it is a FIFO barrier — everything submitted before it has
    completed by the time it returns. Deterministic, and no wall-clock sleep, which is what keeps
    this suite stable on a loaded CI box (the M5 gate's own rule).
    """
    for _ in range(rounds):
        for _ in range(20):
            await asyncio.sleep(0)
        await rig.store._run(lambda: None)  # noqa: SLF001 - a FIFO barrier on the writer thread
        for _ in range(20):
            await asyncio.sleep(0)
        # ⚠️ And a zero-length advance, which is not decoration. FakeClock wakes only the sleepers
        # it *crosses*, so a coroutine that registers its sleep after an advance has already gone
        # by waits forever. The scheduler re-arms after every fire, and whether it gets there
        # before or after the test's advance is a scheduling race — one this suite lost on Linux
        # and won on Windows, which is the worst possible way to find out. advance(0) re-wakes
        # anything already due without moving virtual time.
        await rig.clock.advance(0)


async def _wait_until(
    rig: Rig, pred: Callable[[], bool], *, tries: int = 40, what: str = "condition"
) -> None:
    """Settle repeatedly until ``pred`` holds, or fail saying what never happened.

    A fixed number of settle rounds is a guess about how many hops a fact needs — bus worker,
    handler, writer thread, scheduler re-arm — and a guess that is right on one OS and wrong on
    another is the worst kind: these two tests passed on Windows and failed on Linux CI. Waiting on
    the condition instead removes the guess. It still cannot hang: the cap turns a real regression
    into a named failure rather than a timeout.
    """
    for _ in range(tries):
        if pred():
            return
        await _settle(rig, rounds=1)
    raise AssertionError(f"{what} never happened")


def _env(rig: Rig) -> dict[str, object]:
    return envelope(clock=rig.clock, correlation_id=uuid4(), source="test")


async def _seed_routine(rig: Rig, *, local_time: str = "08:00") -> int:
    """Insert a routine fact + its routines row directly, and return the fact id.

    Writing facts is ``FactRepository``'s job, so the fixture reaches past the port for its
    precondition rather than pretending this service owns a write it does not.
    """

    def _insert() -> int:
        conn = rig.store._conn_sync()  # noqa: SLF001 - fixture precondition
        with conn:
            cur = conn.execute(
                "INSERT INTO facts (text, kind, importance, created_at, last_accessed_at) "
                "VALUES ('coffee', 'routine', 6, ?, ?)",
                (_START, _START),
            )
            fact_id = int(cur.lastrowid or 0)
            conn.execute(
                "INSERT INTO routines (fact_id, rrule, local_time, timezone) "
                "VALUES (?, 'FREQ=DAILY', ?, ?)",
                (fact_id, local_time, _ZONE),
            )
        return fact_id

    return await rig.store._run(_insert)  # noqa: SLF001 - as above


async def _make_deliverable(rig: Rig) -> None:
    """Put the world in the state UC-03 assumes: awake, someone present, nothing said today."""
    await rig.state.transition(Trigger.SYSTEM_STARTED, correlation_id=uuid4())
    await rig.bus.publish(VisionPresenceGained(**_env(rig), confidence=0.9))  # type: ignore[arg-type]
    await _settle(rig)


# ── Registration (AC-2) ──────────────────────────────────────────────────────────────────────


async def test_a_stored_routine_becomes_a_scheduled_trigger(rig: Rig) -> None:
    """§3.7.3's zero-coupling claim, exercised: ``MemoryService`` publishes a fact and a schedule
    appears, with neither service importing the other."""
    fact_id = await _seed_routine(rig)
    await rig.bus.publish(
        MemoryFactStored(**_env(rig), fact_id=fact_id, kind="routine", importance=6)  # type: ignore[arg-type]
    )
    await _settle(rig)

    triggers = await rig.store.enabled_triggers()
    assert [t.fact_id for t in triggers] == [fact_id]
    assert triggers[0].next_fire_at is not None


async def test_a_non_routine_fact_schedules_nothing(rig: Rig) -> None:
    """Filtering on ``kind`` from the payload is why §9.1.3 puts it there — no store read needed
    to ignore the overwhelming majority of facts."""
    await rig.bus.publish(
        MemoryFactStored(**_env(rig), fact_id=1, kind="preference", importance=4)  # type: ignore[arg-type]
    )
    await _settle(rig)
    assert await rig.store.enabled_triggers() == []


async def test_a_routine_with_no_schedule_is_not_an_error(rig: Rig) -> None:
    """Not every routine has a clock time. It schedules nothing and says so in the log rather than
    raising — but it *is* said, because silence here is indistinguishable from a model that has
    stopped filling ``remember_fact``'s schedule (#310's shape)."""

    def _insert() -> int:
        conn = rig.store._conn_sync()  # noqa: SLF001 - fixture precondition
        with conn:
            cur = conn.execute(
                "INSERT INTO facts (text, kind, importance, created_at, last_accessed_at) "
                "VALUES ('runs sometimes', 'routine', 4, ?, ?)",
                (_START, _START),
            )
        return int(cur.lastrowid or 0)

    fact_id = await rig.store._run(_insert)  # noqa: SLF001 - as above
    await rig.bus.publish(
        MemoryFactStored(**_env(rig), fact_id=fact_id, kind="routine", importance=4)  # type: ignore[arg-type]
    )
    await _settle(rig)
    assert await rig.store.enabled_triggers() == []


async def test_deleting_the_fact_removes_the_schedule(rig: Rig) -> None:
    """UC-07's hard delete. A schedule the user withdrew consent for must not go off — a privacy
    property, not a convenience one."""
    fact_id = await _seed_routine(rig)
    await rig.bus.publish(
        MemoryFactStored(**_env(rig), fact_id=fact_id, kind="routine", importance=6)  # type: ignore[arg-type]
    )
    await _settle(rig)
    await rig.bus.publish(MemoryFactDeleted(**_env(rig), fact_id=fact_id))  # type: ignore[arg-type]
    await _settle(rig)
    assert await rig.store.enabled_triggers() == []


async def test_the_heap_is_rebuilt_from_the_store_on_boot(rig: Rig) -> None:
    """AC-3. What makes M7's *"restart the process, recall all 20"* true of schedules as well as
    facts: everything that does not survive a restart is a cache, and a routine the user gave us
    last week is not a cache."""
    fact_id = await _seed_routine(rig)
    trigger_id = await rig.store.upsert_routine_trigger(
        fact_id, next_fire_at=_START + 60, cooldown_s=900, at=_START
    )
    await rig.bus.publish(SystemStarted(**_env(rig), adapters={}))  # type: ignore[arg-type]
    await _settle(rig)

    await _make_deliverable(rig)
    await rig.clock.advance(60)
    await _wait_until(
        rig,
        lambda: any(isinstance(e, BehaviorTriggerFired) for e in rig.events),
        what="the rebuilt trigger firing",
    )

    fired = [e for e in rig.events if isinstance(e, BehaviorTriggerFired)]
    assert [e.trigger_id for e in fired] == [trigger_id]


# ── The decision (AC-5/AC-6/AC-7) ────────────────────────────────────────────────────────────


async def test_a_passing_proposal_mints_a_turn_and_moves_the_machine(rig: Rig) -> None:
    """The head of a proactive turn: ``behavior.trigger_fired`` carries a **fresh**
    ``correlation_id`` (§9.1.1 — one of exactly two minting sites), and the state machine moves
    ``IDLE → THINKING`` through a direct call, because ``StateManager`` subscribes to nothing."""
    fact_id = await _seed_routine(rig)
    trigger_id = await rig.store.upsert_routine_trigger(
        fact_id, next_fire_at=_START + 60, cooldown_s=900, at=_START
    )
    await rig.bus.publish(SystemStarted(**_env(rig), adapters={}))  # type: ignore[arg-type]
    await _make_deliverable(rig)
    assert rig.state.state is RobotState.IDLE

    await rig.clock.advance(60)
    await _wait_until(
        rig,
        lambda: any(isinstance(e, BehaviorTriggerFired) for e in rig.events),
        what="the proposal firing",
    )

    fired = [e for e in rig.events if isinstance(e, BehaviorTriggerFired)]
    assert len(fired) == 1
    assert fired[0].trigger_id == trigger_id
    assert fired[0].fact_id == fact_id
    assert rig.state.state is RobotState.THINKING

    delivered = [e for e in rig.events if isinstance(e, BehaviorProactiveDelivered)]
    assert delivered and delivered[0].correlation_id == fired[0].correlation_id


async def test_a_vetoed_proposal_is_silent_but_never_unrecorded(rig: Rig) -> None:
    """Rule 3: nobody is there. No turn, no state move — and a row, because §10.6's whole argument
    is that a robot that never fires and a robot vetoed forty times look identical without one."""
    fact_id = await _seed_routine(rig)
    await rig.store.upsert_routine_trigger(
        fact_id, next_fire_at=_START + 60, cooldown_s=900, at=_START
    )
    await rig.bus.publish(SystemStarted(**_env(rig), adapters={}))  # type: ignore[arg-type]
    await rig.state.transition(Trigger.SYSTEM_STARTED, correlation_id=uuid4())
    await _settle(rig)  # deliberately no presence

    await rig.clock.advance(60)
    await _wait_until(
        rig,
        lambda: any(isinstance(e, BehaviorProactiveSuppressed) for e in rig.events),
        what="the proposal being vetoed",
    )

    assert [e for e in rig.events if isinstance(e, BehaviorTriggerFired)] == []
    assert rig.state.state is RobotState.IDLE
    vetoed = [e for e in rig.events if isinstance(e, BehaviorProactiveSuppressed)]
    assert [e.rule for e in vetoed] == ["presence"]


async def test_every_proposal_writes_exactly_one_row(rig: Rig) -> None:
    """⚠️ AC-6, and the highest-value assertion in this file.

    Row count equals proposal count **regardless of outcome**. §10.6: *"every considered proposal
    is logged, delivered or not"* — and the reason is that R-08 is the risk nobody can see
    happening. A suppression that returns early instead of logging costs nothing today and costs
    the entire tuning story in month three, when someone asks "is it under-firing?" and the only
    honest answer is a shrug.
    """
    fact_id = await _seed_routine(rig)
    trigger_id = await rig.store.upsert_routine_trigger(
        fact_id, next_fire_at=_START + 60, cooldown_s=900, at=_START
    )
    await rig.bus.publish(SystemStarted(**_env(rig), adapters={}))  # type: ignore[arg-type]
    await rig.state.transition(Trigger.SYSTEM_STARTED, correlation_id=uuid4())
    # Settle before scheduling: the boot rebuild re-registers this trigger at its *stored*
    # next_fire_at, and a rebuild landing after the test's own schedule() would silently overwrite
    # it and swallow the first proposal.
    await _settle(rig)

    proposals = 0
    # One suppressed (nobody is there), then one delivered (presence arrives), then one suppressed
    # again — and the third reason is `state`, not `cooldown`, which is the interesting part: the
    # delivery moved the machine IDLE -> THINKING, so rule 2 vetoes before rule 5 ever gets a look.
    # First-veto-wins is not an abstraction here; it decides what §10.6's histogram records.
    for step in range(3):
        if step == 1:
            await rig.bus.publish(
                VisionPresenceGained(**_env(rig), confidence=0.9)  # type: ignore[arg-type]
            )
            await _settle(rig)
        rig.behavior._scheduler.schedule(  # noqa: SLF001 - re-arming is #241's job, not this test's
            trigger_id, fire_at=rig.clock.now() + 10
        )
        await _settle(rig)
        await rig.clock.advance(10)
        expected = proposals + 1
        await _wait_until(
            rig,
            lambda n=expected: (
                len(  # type: ignore[misc]
                    [
                        e
                        for e in rig.events
                        if isinstance(
                            e, (BehaviorProactiveDelivered, BehaviorProactiveSuppressed)
                        )
                    ]
                )
                >= n
            ),
            what=f"proposal {expected} being decided",
        )
        proposals += 1

    def _rows() -> list[tuple[str, str | None]]:
        conn = rig.store._conn_sync()  # noqa: SLF001 - asserting a table the port has no read for
        return [
            (row["outcome"], row["reason"])
            for row in conn.execute(
                "SELECT outcome, reason FROM proactive_log ORDER BY id"
            )
        ]

    rows = await rig.store._run(_rows)  # noqa: SLF001 - as above
    assert len(rows) == proposals, "one row per proposal, whatever the verdict"
    assert rows[0] == ("suppressed", "presence")
    assert rows[1] == ("delivered", None)
    assert rows[2] == ("suppressed", "state")


async def test_the_log_row_and_the_event_agree_on_the_rule(rig: Rig) -> None:
    """The same fact told two ways. §10.6 groups by the column and anything live reads the event;
    if they disagreed, the histogram and the stream would tell different stories about one veto."""
    fact_id = await _seed_routine(rig)
    await rig.store.upsert_routine_trigger(
        fact_id, next_fire_at=_START + 60, cooldown_s=900, at=_START
    )
    await rig.bus.publish(SystemStarted(**_env(rig), adapters={}))  # type: ignore[arg-type]
    await rig.state.transition(Trigger.SYSTEM_STARTED, correlation_id=uuid4())
    await _settle(rig)
    await rig.clock.advance(60)
    await _wait_until(
        rig,
        lambda: any(isinstance(e, BehaviorProactiveSuppressed) for e in rig.events),
        what="the veto",
    )

    def _reason() -> str:
        conn = rig.store._conn_sync()  # noqa: SLF001 - as above
        return str(
            conn.execute("SELECT reason FROM proactive_log").fetchone()["reason"]
        )

    vetoed = [e for e in rig.events if isinstance(e, BehaviorProactiveSuppressed)]
    assert vetoed[0].rule == await rig.store._run(_reason)  # noqa: SLF001 - as above


# ── Context assembly ─────────────────────────────────────────────────────────────────────────


async def test_presence_age_survives_the_person_leaving(rig: Rig) -> None:
    """``presence_lost`` ages from the **last sighting**, not from the moment the filter concluded
    they had gone — ``absent_for_s`` is measured from the last positive detection (§9.1.3), so
    using "now" would silently give rule 3 an extra ``lose_window_s`` of credit."""
    await _make_deliverable(rig)
    context = await rig.behavior._context(  # noqa: SLF001 - asserting assembly, which has no public surface
        now=rig.clock.now(), trigger_last_fired_s=math.inf, trigger_cooldown_s=900.0
    )
    assert context.presence_age_s == pytest.approx(0.0, abs=1.0)
    assert context.state is RobotState.IDLE


async def test_ambient_speech_counts_only_what_never_became_a_conversation(
    rig: Rig,
) -> None:
    """Rule 4 end to end across two events: the VAD hears 90 s, a transcript arrives for it, and
    the accumulator drops it. Counting it would suppress the next proactive turn for the crime of
    having had a conversation."""
    corr = uuid4()
    await rig.bus.publish(
        AudioSpeechEnded(
            **{**_env(rig), "correlation_id": corr},  # type: ignore[arg-type]
            duration_ms=90_000,
        )
    )
    await _settle(rig)
    before = await rig.behavior._context(  # noqa: SLF001 - as above
        now=rig.clock.now(), trigger_last_fired_s=math.inf, trigger_cooldown_s=900.0
    )
    assert before.ambient_speech_s == pytest.approx(90.0)

    await rig.bus.publish(
        ConversationUserTranscribed(
            **{**_env(rig), "correlation_id": corr},  # type: ignore[arg-type]
            text="hello",
            is_approximate=False,
        )
    )
    await _settle(rig)
    after = await rig.behavior._context(  # noqa: SLF001 - as above
        now=rig.clock.now(), trigger_last_fired_s=math.inf, trigger_cooldown_s=900.0
    )
    assert after.ambient_speech_s == pytest.approx(0.0)


async def test_the_daily_budget_resets_at_local_midnight_not_on_a_rolling_day(
    rig: Rig,
) -> None:
    """Rule 6 is a *daily* budget. A rolling 24 hours would let five deliveries at 23:00 silence
    the whole of the next morning — the one part of the day proactivity exists for."""
    day_start = rig.behavior._local_day_start(rig.clock.now())  # noqa: SLF001 - as above
    for offset in (-3600, 60):  # one yesterday, one today
        await rig.store.record(
            trigger_id=None,
            considered_at=day_start + offset,
            outcome="delivered",
            reason=None,
            utterance="x",
        )
    context = await rig.behavior._context(  # noqa: SLF001 - as above
        now=rig.clock.now(), trigger_last_fired_s=math.inf, trigger_cooldown_s=900.0
    )
    assert context.delivered_today == 1


async def test_the_budget_and_the_cooldown_survive_a_restart(rig: Rig) -> None:
    """Rules 5 and 6 are the only two with memory, and they read it from the database rather than
    an in-process counter — otherwise a reboot at 07:00 resets the day's budget and the robot
    becomes five times more talkative every time it restarts."""
    await rig.store.record(
        trigger_id=None,
        considered_at=rig.clock.now() - 60,
        outcome="delivered",
        reason=None,
        utterance="x",
    )
    fresh = BehaviorService(
        bus=rig.bus,
        clock=rig.clock,
        state=rig.state,
        triggers=rig.store,
        proactive_log=rig.store,
        limits=_LIMITS,
        timezone=_ZONE,
        default_cooldown_s=900,
        hold_open_s=30.0,
        ignore_backoff_multiplier=2,
        ignore_streak_limit=3,
    )
    context = await fresh._context(  # noqa: SLF001 - as above
        now=rig.clock.now(), trigger_last_fired_s=math.inf, trigger_cooldown_s=900.0
    )
    assert context.last_proactive_s == pytest.approx(60.0)
    assert context.delivered_today == 1


# ── Shape ────────────────────────────────────────────────────────────────────────────────────


async def test_every_subscription_is_named_for_the_drift_check(rig: Rig) -> None:
    """``name`` is mandatory so §9.1.5's check can see the subscriber — an anonymous handler is
    invisible to it. Twelve, per §9.1.3, not the three §10's prose implies."""
    names = {sub.name for sub in rig.behavior.subscriptions()}
    assert len(names) == 12
    assert all(name.startswith("BehaviorService.") for name in names)


async def test_stop_is_idempotent(rig: Rig) -> None:
    await rig.behavior.stop()
    await rig.behavior.stop()


# -- 10.5: the robot notices it is being ignored (#241) --------------------------------------


async def _deliver_once(rig: Rig, *, trigger_id: int) -> None:
    """Put one proposal through the gate and let it be delivered."""
    before = len([e for e in rig.events if isinstance(e, BehaviorProactiveDelivered)])
    rig.behavior._scheduler.schedule(  # noqa: SLF001 - re-arming by hand keeps the arc explicit
        trigger_id, fire_at=rig.clock.now() + 10
    )
    await _settle(rig)
    await rig.clock.advance(10)
    await _wait_until(
        rig,
        lambda: (
            len([e for e in rig.events if isinstance(e, BehaviorProactiveDelivered)])
            > before
        ),
        what="a delivery",
    )
    # ⚠️ And wait for the reply window to actually be armed. `proactive_delivered` is published
    # *before* the turn is recorded as pending, so a test that advanced the clock here would race
    # the arming — the sleeper would register after the advance, never be crossed, and
    # `_pending is None` would then read as "the window closed" when it means "never opened". A
    # predicate that cannot tell *not yet* from *done* is not a predicate. That is exactly the bug
    # the first draft of this helper shipped.
    await _wait_until(
        rig,
        lambda: rig.behavior._pending is not None,  # noqa: SLF001 - the arming IS the event
        what="the reply window being armed",
    )
    # ...and one more settle so the spawned window task actually *runs* and registers its sleep.
    # `_pending` is assigned before `spawn()`, so the flag can be true while the coroutine has not
    # started — and a FakeClock only wakes the sleepers it crosses, so an advance landing in that
    # gap leaves the window sleeping forever.
    await _settle(rig)


async def _reaction(rig: Rig, log_id: int = 1) -> str | None:
    def _read() -> str | None:
        row = (
            rig.store._conn_sync()
            .execute(  # noqa: SLF001 - the port has no read for this column
                "SELECT user_reaction FROM proactive_log WHERE id = ?", (log_id,)
            )
            .fetchone()
        )
        return None if row is None else row["user_reaction"]

    return await rig.store._run(_read)  # noqa: SLF001 - as above


async def test_silence_widens_the_cooldown_and_advances_the_streak(rig: Rig) -> None:
    """AC-1. 10.5: *"Without this, a badly-conceived trigger annoys forever at a fixed rate. With
    it, the robot notices it is being ignored and stops."*"""
    fact_id = await _seed_routine(rig)
    trigger_id = await rig.store.upsert_routine_trigger(
        fact_id, next_fire_at=None, cooldown_s=900, at=_START
    )
    await rig.bus.publish(SystemStarted(**_env(rig), adapters={}))  # type: ignore[arg-type]
    await _make_deliverable(rig)
    await _deliver_once(rig, trigger_id=trigger_id)

    await rig.clock.advance(30)  # the hold-open window, unanswered
    await _wait_until(
        rig,
        lambda: rig.behavior.resolved_deliveries == 1,
        what="the reply window closing",
    )

    record = await rig.store.get(trigger_id)
    assert record is not None
    assert record.ignore_streak == 1
    assert record.cooldown_s == 1800, "cooldown_s *= ignore_backoff_multiplier"
    assert await _reaction(rig) == "ignored"


async def test_a_reply_resets_the_streak_rather_than_decrementing_it(rig: Rig) -> None:
    """AC-2, and *reset* is the design: one answered reminder means the trigger is wanted, so
    making the user earn back three days of goodwill would be a different, worse robot.

    The reply is matched by **timing, not by correlation_id**, and it cannot be otherwise: a reply
    is a fresh utterance, so AudioService mints a new id for it at the other turn origin. The gate
    guarantees no second proactive turn is in flight, so any speech in the window answers this one.
    """
    fact_id = await _seed_routine(rig)
    trigger_id = await rig.store.upsert_routine_trigger(
        fact_id, next_fire_at=None, cooldown_s=900, at=_START
    )
    await rig.store.set_backoff(trigger_id, ignore_streak=2, cooldown_s=3600)
    await rig.bus.publish(SystemStarted(**_env(rig), adapters={}))  # type: ignore[arg-type]
    await _make_deliverable(rig)
    await _deliver_once(rig, trigger_id=trigger_id)

    await rig.bus.publish(AudioSpeechStarted(**_env(rig), ring_buffer_ms=300))  # type: ignore[arg-type]
    await _wait_until(
        rig,
        lambda: rig.behavior.resolved_deliveries == 1,
        what="the reply being noticed",
    )

    record = await rig.store.get(trigger_id)
    assert record is not None
    assert record.ignore_streak == 0, "reset, not decremented"
    assert record.cooldown_s == 900, "and the cooldown returns to its configured value"
    assert await _reaction(rig) == "engaged"


async def test_the_third_ignore_switches_the_trigger_off_loudly(rig: Rig) -> None:
    """AC-3: at the limit the trigger disables itself, on the exact transition, and says so.

    10.5: *"Disabling is logged loudly, never silent. A trigger that turned itself off is
    diagnostic information about the design, and if you do not surface it you will never learn
    which of your ideas were bad."*
    """
    fact_id = await _seed_routine(rig)
    trigger_id = await rig.store.upsert_routine_trigger(
        fact_id, next_fire_at=None, cooldown_s=900, at=_START
    )
    await rig.store.set_backoff(trigger_id, ignore_streak=2, cooldown_s=3600)
    await rig.bus.publish(SystemStarted(**_env(rig), adapters={}))  # type: ignore[arg-type]
    await _make_deliverable(rig)
    await _deliver_once(rig, trigger_id=trigger_id)

    await rig.clock.advance(30)
    await _wait_until(
        rig,
        lambda: any(isinstance(e, BehaviorTriggerDisabled) for e in rig.events),
        what="the trigger disabling itself",
    )

    disabled = [e for e in rig.events if isinstance(e, BehaviorTriggerDisabled)]
    assert [(e.trigger_id, e.ignore_streak) for e in disabled] == [(trigger_id, 3)]
    record = await rig.store.get(trigger_id)
    assert record is not None
    assert record.enabled is False


async def test_a_disabled_trigger_stays_disabled_across_a_restart(rig: Rig) -> None:
    """AC-4, and the reason the streak is a *column* rather than a field: a backoff that resets on
    reboot is not a backoff. Proven through the boot rebuild's own query."""
    fact_id = await _seed_routine(rig)
    trigger_id = await rig.store.upsert_routine_trigger(
        fact_id, next_fire_at=_START + 60, cooldown_s=900, at=_START
    )
    await rig.behavior._disable(trigger_id, ignore_streak=3)  # noqa: SLF001 - the arc under test

    fresh = BehaviorService(
        bus=rig.bus,
        clock=rig.clock,
        state=rig.state,
        triggers=rig.store,
        proactive_log=rig.store,
        limits=_LIMITS,
        timezone=_ZONE,
        default_cooldown_s=900,
        hold_open_s=30.0,
        ignore_backoff_multiplier=2,
        ignore_streak_limit=3,
    )
    assert await rig.store.enabled_triggers() == []
    await fresh._on_started(  # noqa: SLF001 - the boot rebuild, called directly
        SystemStarted(**_env(rig), adapters={})  # type: ignore[arg-type]
    )
    assert fresh._scheduler.pending == 0  # noqa: SLF001 - nothing was restored


async def test_two_ignores_then_a_reply_leaves_no_residue(rig: Rig) -> None:
    """The sequence AC-2 asks for: ignored, ignored, replied.

    Driven through the resolution path directly rather than by staging three policy-passing
    deliveries, and that is a deliberate choice rather than a shortcut. Three real deliveries would
    need the global cooldown, the per-trigger cooldown and the state arc all stepped around, and
    every one of those steps is a chance for the test to prove something about the *scaffolding*.
    The gate is tested exhaustively in ``tests/domain/test_behavior.py``; what is under test here is
    the arithmetic §10.5 specifies.

    The streak must end at zero **and** the cooldown back at its configured value: a reset that
    left the cooldown at 4x would keep punishing a trigger the user has just shown they want.
    """
    fact_id = await _seed_routine(rig)
    trigger_id = await rig.store.upsert_routine_trigger(
        fact_id, next_fire_at=None, cooldown_s=900, at=_START
    )

    async def _resolve(*, engaged: bool) -> None:
        record = await rig.store.get(trigger_id)
        assert record is not None
        log_id = await rig.store.record(
            trigger_id=trigger_id,
            considered_at=rig.clock.now(),
            outcome="delivered",
            reason=None,
            utterance=None,
        )
        rig.behavior._pending = _PendingDelivery(  # noqa: SLF001 - the arc under test
            trigger_id=trigger_id,
            log_id=log_id,
            ignore_streak=record.ignore_streak,
            cooldown_s=record.cooldown_s,
        )
        await rig.behavior._resolve_pending(engaged=engaged)  # noqa: SLF001 - as above

    await _resolve(engaged=False)
    await _resolve(engaged=False)
    record = await rig.store.get(trigger_id)
    assert record is not None
    assert (record.ignore_streak, record.cooldown_s) == (2, 3600)

    await _resolve(engaged=True)
    record = await rig.store.get(trigger_id)
    assert record is not None
    assert (record.ignore_streak, record.cooldown_s) == (0, 900)
