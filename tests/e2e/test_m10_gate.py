"""M10 gate — UC-03 end to end, unprompted, in milliseconds (#315, SDS §14.5).

SDS §14.5 does not merely suggest this test. It says the milestone's gate **is** it:

> *"That is UC-03 — the entire product pitch — as a test that runs in about 40 milliseconds on a
> laptop with no hardware, no network and no API key. […] M10's gate criterion reads 'the coffee
> scenario, end to end, unprompted.' **This test is that criterion, mechanised**, runnable on every
> commit."*

It did not exist. `#245` is the *live* multi-morning gate; nothing covered the mechanised one, which
is the difference between the multi-morning run being a **confirmation** and being the first time the
whole chain has ever executed end to end.

Three things §14.5 says make it possible, all built long before M10 and all for other reasons:
``Clock`` is a port, so ``advance_to("07:55")`` replaces waiting until morning; ``ReplayRealtimeClient``
gives deterministic replies with no key; and every adapter is config-switched, so the same services
run here as on the Pi.

⚠️ **The restart is a real one.** The store is a **file** under ``tmp_path`` and the second half of
the arc builds a fresh set of services against it. §14.5: *"Everything that doesn't survive it isn't
memory — it's a cache."* A routine the user gave us last week is not a cache.

⚠️ **The harness's own pass/fail logic is under test** (CLAUDE.md §7.1, and M4's lesson that *a gate
that can pass on silence is not a gate*). Each criterion has a companion that neuters exactly one
guard and asserts the criterion goes **red**.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from avid.adapters import (
    FakeEmbedder,
    FakeSpeaker,
    FakeTextModel,
    FakeTurnSink,
    HybridRetriever,
)
from avid.adapters.clock import FakeClock
from avid.adapters.fact_repository import SqliteFactRepo
from avid.adapters.realtime import ReplayRealtimeClient
from avid.adapters.trigger_store import SqliteTriggerStore
from avid.core.envelope import envelope
from avid.core.event_bus import AsyncioEventBus
from avid.core.state_manager import StateManager
from avid.domain import (
    BehaviorProactiveSuppressed,
    BehaviorTriggerFired,
    ConversationAssistantResponded,
    ConversationTurnStarted,
    Event,
    PolicyLimits,
    RoutineSpec,
    ScoreWeights,
    SystemStarted,
    Trigger,
    VisionPresenceGained,
)
from avid.services.behavior import BehaviorService
from avid.services.conversation import ConversationService
from avid.services.cue_bank import CueBank
from avid.services.memory import MemoryService

_SESSIONS = Path(__file__).resolve().parents[2] / "assets" / "sessions"

# UTC throughout: the DST arithmetic has its own exhaustive suite in tests/core/test_schedule.py,
# and a gate that also depended on a transition would be grading two things through one assertion.
_ZONE = "UTC"

_LIMITS = PolicyLimits(
    quiet_start_minutes=22 * 60,
    quiet_end_minutes=7 * 60 + 30,
    presence_window_s=300,
    ambient_speech_threshold_s=60,
    global_cooldown_s=900,
    daily_budget=5,
)


class _GateAffect:
    """An ``AffectTools`` double. The face is M3/M6 ground; this gate grades the clock."""

    async def set_affect(self, affect: object, *, correlation_id: UUID) -> None:
        return None


@dataclass
class Rig:
    """One composition of the real bus and the real services, against a real file."""

    bus: AsyncioEventBus
    clock: FakeClock
    state: StateManager
    memory: MemoryService
    behavior: BehaviorService
    conversation: ConversationService
    sink: FakeTurnSink
    triggers: SqliteTriggerStore
    facts: SqliteFactRepo
    events: list[Event]


async def _compose(db: Path, clock: FakeClock) -> Rig:
    """Build the services the way ``main._wire_services`` builds them, against ``db``.

    Not a mock in sight (SDS §14.3): the real ``AsyncioEventBus``, the real ``StateManager``, the
    real ``MemoryService`` and ``BehaviorService``, and the P6 fakes for the embedder and text model.
    ``ConversationService`` **is** wired, against the ``proactive_coffee`` replay fixture, because
    AC-1 grades the *utterance* and not merely the trigger. A gate that stopped at
    ``behavior.trigger_fired`` would prove the clock reached the behaviour engine and nothing about
    whether the robot said anything.
    """
    bus = AsyncioEventBus(clock=clock)
    state = StateManager(bus=bus, clock=clock)
    facts = SqliteFactRepo(db_path=db, clock=clock)
    triggers = SqliteTriggerStore(db_path=db, clock=clock)
    embedder = FakeEmbedder(dimensions=384)
    retriever = HybridRetriever(
        repo=facts,
        embedder=embedder,
        bus=bus,
        clock=clock,
        top_k=5,
        half_life_days=14.0,
        weights=ScoreWeights(),
    )
    memory = MemoryService(
        bus=bus,
        clock=clock,
        repo=facts,
        retriever=retriever,
        embedder=embedder,
        text_model=FakeTextModel(),
        supersession_threshold=0.92,
        supersession_k=5,
        forget_relevance_floor=0.35,
        forget_k=5,
        top_facts_max=15,
        top_facts_token_budget=600,
    )
    behavior = BehaviorService(
        bus=bus,
        clock=clock,
        state=state,
        triggers=triggers,
        proactive_log=triggers,
        limits=_LIMITS,
        timezone=_ZONE,
        default_cooldown_s=900,
        hold_open_s=30.0,
        ignore_backoff_multiplier=2,
        stale_grace_s=600,
        ignore_streak_limit=3,
    )
    sink = FakeTurnSink()
    conversation = ConversationService(
        bus=bus,
        clock=clock,
        state=state,
        client=ReplayRealtimeClient.from_dir(
            _SESSIONS / "proactive_coffee", clock=clock
        ),
        sink=sink,
        cues=CueBank(speaker=FakeSpeaker(), asset_dir=None),
        memory=memory,
        affect=_GateAffect(),
        behavior=behavior,
        session_idle_close_s=600,
        memory_inject_timeout_s=1.0,
        default_timezone=_ZONE,
        hold_open_s=30.0,
        think_timeout_s=3600.0,
        server_turn_detection=False,
        thinking_delay_ms=0,
    )
    seen: list[Event] = []

    async def _collect(event: Event) -> None:
        seen.append(event)

    for service in (memory, behavior, conversation):
        for sub in service.subscriptions():
            bus.subscribe(
                sub.event_type,
                sub.handler,
                name=sub.name,
                policy=sub.policy,
                maxsize=sub.maxsize,
            )
    for event_type in (
        BehaviorTriggerFired,
        BehaviorProactiveSuppressed,
        ConversationTurnStarted,
        ConversationAssistantResponded,
    ):
        bus.subscribe(event_type, _collect, name=f"gate.{event_type.name}")
    await bus.start()
    await memory.start()
    await behavior.start()
    return Rig(
        bus, clock, state, memory, behavior, conversation, sink, triggers, facts, seen
    )


async def _teardown(rig: Rig) -> None:
    await rig.conversation.stop()
    await rig.behavior.stop()
    await rig.memory.stop()
    await rig.bus.stop()
    await rig.triggers.aclose()


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Path]:
    """A real file, because the restart is the point."""
    yield tmp_path / "robot.db"


async def _settle(rig: Rig, rounds: int = 4) -> None:
    """Let the bus deliver and both store threads finish.

    The FIFO barrier through the single writer pool, plus a zero-length advance so anything already
    due is re-woken — a ``FakeClock`` wakes only the sleepers it crosses, and a coroutine that
    registers after an advance would otherwise wait forever.
    """
    for _ in range(rounds):
        for _ in range(20):
            await asyncio.sleep(0)
        await rig.triggers._run(lambda: None)  # noqa: SLF001 - FIFO barrier on the writer thread
        for _ in range(20):
            await asyncio.sleep(0)
        await rig.clock.advance(0)


async def _wait_until(
    rig: Rig, pred: Callable[[], bool], *, tries: int = 60, what: str = "condition"
) -> None:
    for _ in range(tries):
        if pred():
            return
        await _settle(rig, rounds=1)
    raise AssertionError(f"{what} never happened")


def _env(rig: Rig, correlation_id: UUID | None = None) -> dict[str, object]:
    return envelope(
        clock=rig.clock, correlation_id=correlation_id or uuid4(), source="gate"
    )


async def _wake_and_see_someone(rig: Rig) -> None:
    """Boot to IDLE and put a person in the room — the world UC-03 assumes at 07:55."""
    await rig.state.transition(Trigger.SYSTEM_STARTED, correlation_id=uuid4())
    await rig.bus.publish(VisionPresenceGained(**_env(rig), confidence=0.9))  # type: ignore[arg-type]
    await _settle(rig)


async def _tell_it_about_coffee(rig: Rig) -> int:
    """UC-02: the user says it once, and the model calls ``remember_fact`` with the schedule."""
    return await rig.memory.remember_fact(
        "The user drinks coffee every day at 08:00",
        "routine",
        6,
        schedule=RoutineSpec(rrule="FREQ=DAILY", local_time="08:00", timezone=_ZONE),
    )


# ── The arc ──────────────────────────────────────────────────────────────────────────────────


async def test_uc03_coffee(db: Path) -> None:
    """§14.5's own test, line for line: told once, restarted, fired unprompted the next morning.

    This is the milestone's gate criterion — *"the coffee scenario, end to end, unprompted"* —
    and every step of it costs microseconds because ``Clock`` was made a port for exactly this.
    """
    clock = FakeClock()
    rig = await _compose(db, clock)
    try:
        fact_id = await _tell_it_about_coffee(rig)
        await _settle(rig)

        # The fact became a schedule, and the schedule became a trigger — §3.7.3's zero-coupling
        # claim, with MemoryService and BehaviorService importing nothing of each other.
        routine = await rig.triggers.routine_for(fact_id)
        assert routine is not None
        assert (routine.rrule, routine.local_time) == ("FREQ=DAILY", "08:00")
        assert [t.fact_id for t in await rig.triggers.enabled_triggers()] == [fact_id]
    finally:
        await _teardown(rig)

    # ── restart: a genuinely new composition against the same file ──────────────────────────
    rig = await _compose(db, clock)
    try:
        await rig.bus.publish(SystemStarted(**_env(rig), adapters={}))  # type: ignore[arg-type]
        await _wait_until(
            rig,
            lambda: rig.behavior._scheduler.pending == 1,  # noqa: SLF001 - the rebuild IS the claim
            what="the trigger being rebuilt from the store",
        )

        # ⚠️ The person arrives at 07:50, not at midnight. Rule 3 vetoes unless presence is
        # newer than presence_window_s (300 s), so a test that "saw someone" eight hours before
        # the trigger would be vetoed by rule 3 and prove nothing about the schedule. This is the
        # morning as it actually happens: the user walks in, and five minutes later the robot
        # mentions coffee.
        await rig.clock.advance_to("07:50")
        await _wake_and_see_someone(rig)
        await rig.clock.advance_to("07:55")
        await _wait_until(
            rig,
            lambda: any(isinstance(e, BehaviorTriggerFired) for e in rig.events),
            what="the coffee reminder firing",
        )

        fired = [e for e in rig.events if isinstance(e, BehaviorTriggerFired)]
        assert [e.fact_id for e in fired] == [fact_id]

        # AC-1: the robot actually *said something*, and the turn is marked as the robot's own.
        # Stopping at trigger_fired would prove the clock reached the behaviour engine and nothing
        # about whether anyone would have heard a word.
        # The replay timeline is paced on the injected clock (the fixture's first event is
        # 100 ms in), so the reply needs virtual time to elapse — the same 100 ms a real model
        # would have spent. Stepped rather than jumped, so an assertion cannot be satisfied by a
        # single enormous advance that also blows past every other deadline in the system.
        for _ in range(20):
            if any(isinstance(e, ConversationAssistantResponded) for e in rig.events):
                break
            await rig.clock.advance(0.1)
            await _settle(rig, rounds=1)
        else:
            raise AssertionError("the robot never spoke")
        (spoken,) = [
            e for e in rig.events if isinstance(e, ConversationAssistantResponded)
        ]
        assert "coffee" in spoken.text.lower()

        started = [e for e in rig.events if isinstance(e, ConversationTurnStarted)]
        assert [e.initiator for e in started] == ["proactive"]
        # One correlation id from the trigger all the way to the utterance (§9.1.1): one grep
        # reconstructs the turn, which is the property the whole envelope exists for.
        assert spoken.correlation_id == fired[0].correlation_id
    finally:
        await _teardown(rig)


async def test_the_fire_time_is_the_lead_time_before_the_routine(db: Path) -> None:
    """07:55, not 08:00. ``routines.lead_time_s`` defaults to 300 precisely so the robot mentions
    coffee *before* the coffee — a reminder that arrives at the moment it describes is a clock."""
    clock = FakeClock()
    rig = await _compose(db, clock)
    try:
        fact_id = await _tell_it_about_coffee(rig)
        await _settle(rig)
        (trigger,) = await rig.triggers.enabled_triggers()
        assert trigger.next_fire_at is not None

        routine = await rig.triggers.routine_for(fact_id)
        assert routine is not None
        assert routine.lead_time_s == 300
        # 07:55 on the clock's own first day: FakeClock starts at midnight, so this is arithmetic
        # a reader can check without a timezone library.
        assert trigger.next_fire_at == clock.now() + (7 * 3600 + 55 * 60)
    finally:
        await _teardown(rig)


# ── The suppression arcs (AC-3) ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("case", "expected_rule"),
    [("quiet_hours", "quiet_hours"), ("nobody_there", "presence")],
)
async def test_a_suppressed_proposal_is_silent_and_logged(
    db: Path, case: str, expected_rule: str
) -> None:
    """Both halves, because §10.6's whole argument is that they are different facts: **no audio**,
    and **a row saying why**. A robot that never fires and one vetoed forty times are
    indistinguishable without the second half."""
    clock = FakeClock()
    rig = await _compose(db, clock)
    try:
        await _tell_it_about_coffee(rig)
        await _settle(rig)
        (trigger,) = await rig.triggers.enabled_triggers()
        assert trigger.next_fire_at is not None

        if case == "quiet_hours":
            # Take the morning's booking off the heap *before* winding the clock past it. Without
            # this the fast-forward to 22:55 crosses 08:00, and the robot rightly reports a
            # `stale` skip first (#339) — a real verdict about a real booking, but not the one this
            # arc is staging. Staging the world is the harness's job; the robot is entitled to
            # notice everything the harness does to it.
            rig.behavior._scheduler.cancel(trigger.id)  # noqa: SLF001 - staging, not the robot
            await rig.clock.advance_to("22:55")
            await _wake_and_see_someone(rig)
            # Move the trigger inside the static window: 23:00, well past 22:00.
            rig.behavior._scheduler.schedule(  # noqa: SLF001 - staging the world, not the robot
                trigger.id, fire_at=clock.now() + 300
            )
            await _settle(rig)
            await rig.clock.advance_to("23:00")
        else:
            # Awake, but nobody has ever been seen: rule 3's presence age is infinite.
            await rig.state.transition(Trigger.SYSTEM_STARTED, correlation_id=uuid4())
            await _settle(rig)
            await rig.clock.advance_to("07:55")

        await _wait_until(
            rig,
            lambda: any(isinstance(e, BehaviorProactiveSuppressed) for e in rig.events),
            what=f"the {expected_rule} veto",
        )

        vetoed = [e for e in rig.events if isinstance(e, BehaviorProactiveSuppressed)]
        assert [e.rule for e in vetoed] == [expected_rule]
        assert [e for e in rig.events if isinstance(e, BehaviorTriggerFired)] == [], (
            "a suppressed proposal must produce no turn at all"
        )

        def _rows() -> list[tuple[str, str | None]]:
            conn = rig.triggers._conn_sync()  # noqa: SLF001 - the port has no read for the log
            return [
                (row["outcome"], row["reason"])
                for row in conn.execute("SELECT outcome, reason FROM proactive_log")
            ]

        assert await rig.triggers._run(_rows) == [  # noqa: SLF001 - as above
            ("suppressed", expected_rule)
        ]
    finally:
        await _teardown(rig)


async def test_set_quiet_suppresses_a_trigger_that_would_otherwise_fire(
    db: Path,
) -> None:
    """§10.4's manual override, end to end at 07:55 — the moment that otherwise delivers.

    Both doors reach this same state: the ``set_quiet`` tool (#243) and ``POST /quiet`` (#244) call
    one method on one port, which is what makes §9.5's "also reachable via set_quiet tool" true
    rather than approximately true.
    """
    clock = FakeClock()
    rig = await _compose(db, clock)
    try:
        await _tell_it_about_coffee(rig)
        await _settle(rig)
        await rig.clock.advance_to("07:50")
        await _wake_and_see_someone(rig)

        await rig.behavior.set_quiet(12 * 3600, correlation_id=uuid4())
        await rig.clock.advance_to("07:55")
        await _wait_until(
            rig,
            lambda: any(isinstance(e, BehaviorProactiveSuppressed) for e in rig.events),
            what="the quiet-hours veto",
        )

        vetoed = [e for e in rig.events if isinstance(e, BehaviorProactiveSuppressed)]
        assert [e.rule for e in vetoed] == ["quiet_hours"]
        assert [e for e in rig.events if isinstance(e, BehaviorTriggerFired)] == []
    finally:
        await _teardown(rig)


# ── The gate's own pass/fail logic (AC-5) ────────────────────────────────────────────────────
#
# CLAUDE.md §7.1, and M4's lesson: *a gate that can pass on silence is not a gate*. Each of these
# breaks exactly one guard and asserts the criterion above goes red. A criterion that cannot fail
# is a criterion that proves nothing, and the only way to know is to make it fail on purpose.


async def test_the_gate_fails_if_the_trigger_never_reaches_the_heap(db: Path) -> None:
    """Neuter the boot rebuild. The fire criterion must go red — if it stayed green, the arc was
    being satisfied by something other than the schedule surviving a restart."""
    clock = FakeClock()
    rig = await _compose(db, clock)
    try:
        await _tell_it_about_coffee(rig)
        await _settle(rig)
        rig.behavior._scheduler._scheduled.clear()  # noqa: SLF001 - the neuter
        rig.behavior._scheduler._heap.clear()  # noqa: SLF001 - the neuter

        await rig.clock.advance_to("07:50")
        await _wake_and_see_someone(rig)
        await rig.clock.advance_to("07:55")
        await _settle(rig, rounds=6)

        assert [e for e in rig.events if isinstance(e, BehaviorTriggerFired)] == [], (
            "with an empty heap nothing can fire — if this passes, the gate is not measuring "
            "the scheduler"
        )
    finally:
        await _teardown(rig)


async def test_the_gate_fails_if_the_clock_never_advances(db: Path) -> None:
    """Neuter the clock. UC-03 is a *timing* claim, and a criterion that passes without time
    passing is measuring the wiring rather than the schedule."""
    clock = FakeClock()
    rig = await _compose(db, clock)
    try:
        await _tell_it_about_coffee(rig)
        await _settle(rig)
        await _wake_and_see_someone(rig)
        await _settle(rig, rounds=6)  # deliberately no advance_to

        assert [e for e in rig.events if isinstance(e, BehaviorTriggerFired)] == []
    finally:
        await _teardown(rig)


async def test_the_gate_fails_if_the_schedule_does_not_survive_the_restart(
    db: Path,
) -> None:
    """Neuter persistence by pointing the second composition at a *different* file.

    §14.5: *"Everything that doesn't survive it isn't memory — it's a cache."* If the arc still
    fired here, the restart in ``test_uc03_coffee`` would be proving nothing.
    """
    clock = FakeClock()
    rig = await _compose(db, clock)
    try:
        await _tell_it_about_coffee(rig)
        await _settle(rig)
    finally:
        await _teardown(rig)

    rig = await _compose(db.parent / "somewhere_else.db", clock)
    try:
        await rig.bus.publish(SystemStarted(**_env(rig), adapters={}))  # type: ignore[arg-type]
        await rig.clock.advance_to("07:50")
        await _wake_and_see_someone(rig)
        await rig.clock.advance_to("07:55")
        await _settle(rig, rounds=6)

        assert rig.behavior._scheduler.pending == 0  # noqa: SLF001 - nothing to rebuild
        assert [e for e in rig.events if isinstance(e, BehaviorTriggerFired)] == []
    finally:
        await _teardown(rig)


async def test_the_gate_fails_if_nothing_writes_the_audit_row(db: Path) -> None:
    """Neuter the instrument. §10.6's table is the only thing that can distinguish "never fired"
    from "fired and was vetoed forty times" — and a zero from an absent instrument reads exactly
    like a real zero, which is the M6 defect family this whole discipline exists for."""
    clock = FakeClock()
    rig = await _compose(db, clock)
    try:
        await _tell_it_about_coffee(rig)
        await _settle(rig)

        def _count() -> int:
            conn = rig.triggers._conn_sync()  # noqa: SLF001 - as above
            return int(conn.execute("SELECT COUNT(*) FROM proactive_log").fetchone()[0])

        assert await rig.triggers._run(_count) == 0  # noqa: SLF001 - nothing considered yet

        await rig.state.transition(Trigger.SYSTEM_STARTED, correlation_id=uuid4())
        await _settle(rig)
        await rig.clock.advance_to("07:55")
        await _wait_until(
            rig,
            lambda: any(isinstance(e, BehaviorProactiveSuppressed) for e in rig.events),
            what="a considered proposal",
        )
        assert await rig.triggers._run(_count) == 1, (  # noqa: SLF001 - as above
            "a considered proposal that wrote no row is R-08 going unmeasured"
        )
    finally:
        await _teardown(rig)


async def test_it_fires_again_the_next_morning(db: Path) -> None:
    """⚠️ The regression that mattered, and the one every other test in this file missed.

    ``SchedulerLoop`` *consumes* a heap entry when it fires it — correctly, a due time is a
    one-shot — so something must book the next one. Nothing did. Every trigger fired **at most once
    per process lifetime**, and the coffee reminder would have gone quiet on the second morning
    with nothing in the log to say why.

    Every test here asserted a single fire, which is exactly the mistake ``domain/state.py``'s own
    comments describe: walking an arc only as far as the row under test. PMP's O3 asks for 7/7
    mornings; this is two of them, which is the smallest number that can tell the difference.
    """
    clock = FakeClock()
    rig = await _compose(db, clock)
    try:
        await _tell_it_about_coffee(rig)
        await _settle(rig)

        for morning in (1, 2):
            await rig.clock.advance_to("07:50")
            await _wake_and_see_someone(rig)
            await rig.clock.advance_to("07:55")
            await _wait_until(
                rig,
                lambda n=morning: (
                    len([e for e in rig.events if isinstance(e, BehaviorTriggerFired)])
                    >= n
                ),
                what=f"the reminder firing on morning {morning}",
            )
            # Back to IDLE the way a finished turn gets there, so rule 2 does not veto tomorrow.
            await rig.state.transition(
                Trigger.AUDIO_PLAYBACK_FINISHED, correlation_id=uuid4()
            )
            await _settle(rig)

        fired = [e for e in rig.events if isinstance(e, BehaviorTriggerFired)]
        assert len(fired) == 2, (
            "a reminder that fires once is a reminder that stopped working"
        )
    finally:
        await _teardown(rig)


async def test_a_suppressed_morning_does_not_retire_the_trigger(db: Path) -> None:
    """The nastier half of the same bug.

    A proposal vetoed by rule 3 — nobody in the room — used to leave the trigger un-armed *and*
    with a stale ``next_fire_at``, so one empty morning retired the reminder permanently. That is
    the inverse of R-08's failure and every bit as fatal: the robot goes quiet, correctly the first
    time and wrongly forever after, and §10.6's log shows a single suppression that looks entirely
    reasonable.
    """
    clock = FakeClock()
    rig = await _compose(db, clock)
    try:
        await _tell_it_about_coffee(rig)
        await _settle(rig)

        # Morning one: awake, but nobody has ever been seen.
        await rig.state.transition(Trigger.SYSTEM_STARTED, correlation_id=uuid4())
        await rig.clock.advance_to("07:55")
        await _wait_until(
            rig,
            lambda: any(isinstance(e, BehaviorProactiveSuppressed) for e in rig.events),
            what="the presence veto",
        )

        # Morning two: someone is there.
        await rig.clock.advance_to("07:50")
        await _wake_and_see_someone(rig)
        await rig.clock.advance_to("07:55")
        await _wait_until(
            rig,
            lambda: any(isinstance(e, BehaviorTriggerFired) for e in rig.events),
            what="the reminder firing the morning after it was suppressed",
        )

        (trigger,) = await rig.triggers.enabled_triggers()
        assert trigger.next_fire_at is not None, (
            "a suppressed trigger must still hold a future occurrence, or the boot rebuild "
            "cannot restore it either"
        )
    finally:
        await _teardown(rig)


async def test_the_proactive_turn_actually_plays_audio(db: Path) -> None:
    """⚠️ The crash the rig found, and the one this file could not have caught before.

    ``AudioService`` mints a turn id in exactly one place — its own VAD rising edge — because until
    M10 every turn began with someone speaking. A proactive turn begins with a clock, so the sink
    receives assistant audio for a turn it never heard start, ``_playing_corr`` is ``None``, and the
    assertion in ``_playback_corr`` kills the conversation pump on the **first chunk**. Live, the
    robot fired its reminder, opened a session, and said nothing.

    This file went green through all of it, because the fixture had a transcript and **no audio**.
    The exclusion was deliberate and reasoned — "playback is M4's and M5's ground" — and it was
    exactly wrong: the thing left out of the fixture was the thing that broke. So the fixture now
    carries a chunk, and this asserts PCM reached the speaker seam.
    """
    clock = FakeClock()
    rig = await _compose(db, clock)
    try:
        await _tell_it_about_coffee(rig)
        await _settle(rig)
        await rig.clock.advance_to("07:50")
        await _wake_and_see_someone(rig)
        await rig.clock.advance_to("07:55")
        await _wait_until(
            rig,
            lambda: any(isinstance(e, BehaviorTriggerFired) for e in rig.events),
            what="the reminder firing",
        )

        for _ in range(30):
            if rig.sink.played:
                break
            await rig.clock.advance(0.1)
            await _settle(rig, rounds=1)
        else:
            raise AssertionError(
                "the reminder fired and no audio ever reached the speaker — the pump is dead"
            )

        assert rig.sink.adopted, (
            "the sink was never told whose turn this is, so the next chunk asserts"
        )
        assert rig.sink.adopted[0] == rig.events[0].correlation_id, (
            "and the id it was told must be the one the trigger minted (§9.1.1)"
        )
    finally:
        await _teardown(rig)
