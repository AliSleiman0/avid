"""``DriveService`` — bounded steps, the edge abort, homing, and the stop that beats a fall (#400).

Real bus, real ``FakeDrive``, real ``FakeEdgeSensor``, real ``FakeClock``; no mocks (SDS §14.3).
Waits are on ``asyncio.Event`` or on the step task itself, never on a guessed sleep.

⚠️ **Leg durations here are milliseconds, deliberately.** ``FakeDrive.run`` sleeps on
``asyncio.sleep`` — it is a device fake, and a wheel turning is not fakeable time — so the
geometry below is chosen so a leg is a few tens of milliseconds of genuine wall clock. Only
the *scheduling* (the idle band, the clear hold) sleeps on the injected ``Clock`` and is driven
in one call.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import AsyncIterator
from pathlib import Path
from typing import NamedTuple
from uuid import UUID, uuid4

import pytest

from avid.adapters.clock import FakeClock
from avid.adapters.drive import FakeDrive
from avid.adapters.edge import FakeEdgeSensor
from avid.core.event_bus import AsyncioEventBus
from avid.core.hal import DriveCapabilities
from avid.domain import (
    BUDGET,
    EDGE,
    FAULT,
    PREEMPTED,
    DriveStepAborted,
    DriveStepCompleted,
    DriveStepStarted,
    Event,
    Gesture,
    RobotState,
    StateTransitioned,
    StepGeometry,
    SystemHandlerFailed,
    Trigger,
)
from avid.services.drive import DriveService

_ARRIVAL_TIMEOUT_S = 2.0

# 1000 mm/s at full duty and 20 mm legs at full duty: a leg is 20 ms of wall clock, ~one
# slice of the fake's 20 ms cadence — long enough to be interrupted, short enough that a
# dozen steps cost the suite nothing. Not the shipped numbers; the *relations* are under test.
_CAPS = DriveCapabilities(mm_per_s_at_full=1000.0)
_GEOMETRY = StepGeometry(
    step_mm=20.0, max_excursion_mm=30.0, speed_frac=1.0, dwell_ms=0
)
_WITH_DWELL = StepGeometry(
    step_mm=20.0, max_excursion_mm=30.0, speed_frac=1.0, dwell_ms=30
)
# A long leg, for the tests that need to interrupt one partway.
_LONG = StepGeometry(step_mm=30.0, max_excursion_mm=30.0, speed_frac=0.05, dwell_ms=0)

_POLL_MS = 5
_CLEAR_HOLD_MS = 50
# The idle band. Wide enough that no test steps by accident, short enough to cross in one call.
_IDLE_MIN_S, _IDLE_MAX_S = 100.0, 200.0


class _Collector:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self._arrived = asyncio.Event()

    async def handle(self, event: Event) -> None:
        self.events.append(event)
        self._arrived.set()

    async def wait_for(self, count: int) -> None:
        async with asyncio.timeout(_ARRIVAL_TIMEOUT_S):
            while len(self.events) < count:
                self._arrived.clear()
                if len(self.events) >= count:
                    return
                await self._arrived.wait()

    def of(self, event_type: type[Event]) -> list[Event]:
        return [event for event in self.events if isinstance(event, event_type)]


class Rig(NamedTuple):
    service: DriveService
    bus: AsyncioEventBus
    drive: FakeDrive
    edge: FakeEdgeSensor
    clock: FakeClock
    collector: _Collector


def _register(bus: AsyncioEventBus, service: DriveService) -> None:
    for sub in service.subscriptions():
        bus.subscribe(
            sub.event_type,
            sub.handler,
            name=sub.name,
            policy=sub.policy,
            maxsize=sub.maxsize,
        )


async def _make_rig(
    *,
    geometry: StepGeometry = _GEOMETRY,
    drive: FakeDrive | None = None,
    idle: bool = True,
) -> tuple[Rig, AsyncioEventBus]:
    clock = FakeClock()
    bus = AsyncioEventBus()
    drive = drive if drive is not None else FakeDrive(capabilities=_CAPS)
    edge = FakeEdgeSensor()
    service = DriveService(
        bus=bus,
        drive=drive,
        edge=edge,
        clock=clock,
        geometry=geometry,
        edge_poll_ms=_POLL_MS,
        edge_clear_hold_ms=_CLEAR_HOLD_MS,
        idle_step_interval_min_s=_IDLE_MIN_S,
        idle_step_interval_max_s=_IDLE_MAX_S,
        rng=random.Random(400),
    )
    collector = _Collector()
    _register(bus, service)
    for event_type in (
        DriveStepStarted,
        DriveStepCompleted,
        DriveStepAborted,
        SystemHandlerFailed,
    ):
        bus.subscribe(event_type, collector.handle, name=f"test.{event_type.__name__}")
    await bus.start()
    await service.start()
    rig = Rig(service, bus, drive, edge, clock, collector)
    if idle:
        await _go(rig, RobotState.IDLE)
    return rig, bus


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    """The wheeled rig, already IDLE — the only state a step may run in."""
    built, bus = await _make_rig()
    try:
        yield built
    finally:
        await built.service.stop()
        await bus.stop()


def _transitioned(*, to: RobotState, corr: UUID | None = None) -> StateTransitioned:
    return StateTransitioned(
        event_id=uuid4(),
        correlation_id=corr or uuid4(),
        timestamp_ms=1,
        monotonic_ns=1,
        source="StateManager",
        from_=RobotState.IDLE,
        to=to,
        trigger=Trigger.PRESENCE_LOST_TIMEOUT,
    )


async def _go(rig: Rig, state: RobotState) -> None:
    """Drive the state feed the service reads, and let the handler run."""
    await rig.bus.publish(_transitioned(to=state))
    for _ in range(6):
        await asyncio.sleep(0)


async def _settle(service: DriveService) -> None:
    """Await the step in flight, if any — the task is the thing to wait on, never a sleep."""
    task = service._task
    if task is None:
        return
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), _ARRIVAL_TIMEOUT_S)
    for _ in range(4):
        await asyncio.sleep(0)


async def _wait_until_moving(rig: Rig) -> None:
    """Wait for the wheels to genuinely be turning — a preemption asserted against a leg that
    never started proves the opposite of what it claims."""
    for _ in range(2000):
        if rig.drive.is_running:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("the wheels never started turning")


async def _wait_for_a_sleeper(clock: FakeClock) -> None:
    """Wait until a coroutine has parked on the fake clock — advancing before then wakes
    nothing (the trap ``test_motion.py`` and ``test_conversation.py`` both document)."""
    for _ in range(2000):
        if clock.sleepers:
            return
        await asyncio.sleep(0)
    raise AssertionError("nothing ever slept on the fake clock")


def _headings(rig: Rig) -> list[str]:
    return ["fwd" if left > 0 else "back" for left, _, _ in rig.drive.runs]


# --- the step, end to end -------------------------------------------------------------------


async def test_a_step_goes_out_and_comes_back_and_the_two_odometers_agree(
    rig: Rig,
) -> None:
    """The feature in one test: out, back, and the robot is where it started (AC-4).

    Asserted on the adapter's odometer *and* the service's, because they are integrated from
    different sides of the port — the fake from the slices it ran, the service from the ms the
    port reported — and their agreement is the claim. Neuter: make ``FakeDrive.run`` return the
    planned duration regardless of what it drove, and the service's odometer still reads zero
    while the fake's does not."""
    await rig.service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
    await rig.collector.wait_for(2)
    await _settle(rig.service)

    assert _headings(rig) == ["fwd", "back"]
    assert rig.drive.odometer_mm == pytest.approx(0.0, abs=0.5)
    assert rig.service.offset_mm == pytest.approx(0.0, abs=0.5)
    started = rig.collector.of(DriveStepStarted)
    completed = rig.collector.of(DriveStepCompleted)
    assert [(e.gesture, e.heading, e.distance_mm) for e in started] == [  # type: ignore[attr-defined]
        ("step_toward", "forward", 20.0)
    ]
    assert [(e.gesture, e.net_mm) for e in completed] == [("step_toward", 0.0)]  # type: ignore[attr-defined]
    assert rig.service.steps_performed == 1


async def test_step_back_leads_with_the_backward_leg(rig: Rig) -> None:
    await rig.service.perform(Gesture.STEP_BACK, correlation_id=uuid4())
    await rig.collector.wait_for(2)
    await _settle(rig.service)

    assert _headings(rig) == ["back", "fwd"]
    assert rig.service.offset_mm == pytest.approx(0.0, abs=0.5)


async def test_a_dwell_is_a_pause_between_the_legs_not_a_run() -> None:
    """The dwell is physical time — a pause with the wheels stopped — so it is a wait, never a
    ``run(0, 0)`` that would appear in the trace as a leg."""
    built, bus = await _make_rig(geometry=_WITH_DWELL)
    try:
        await built.service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await built.collector.wait_for(2)
        await _settle(built.service)

        assert len(built.drive.runs) == 2
        assert _headings(built) == ["fwd", "back"]
    finally:
        await built.service.stop()
        await bus.stop()


async def test_a_servo_gesture_is_a_no_op_here(rig: Rig) -> None:
    """A nod is not a step. Nothing runs, nothing is published, nothing is counted."""
    await rig.service.perform(Gesture.NOD, correlation_id=uuid4())
    await _settle(rig.service)

    assert rig.drive.runs == []
    assert rig.collector.events == []


async def test_a_rig_without_wheels_never_moves() -> None:
    """``capabilities is None`` is *no wheels* (SDS §3.9.3): the identical service on a laptop
    or a wheel-less rig plans nothing and publishes nothing."""
    built, bus = await _make_rig(drive=FakeDrive(capabilities=None))
    try:
        await built.service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await _settle(built.service)

        assert built.drive.runs == []
        assert built.collector.events == []
    finally:
        await built.service.stop()
        await bus.stop()


# --- only in IDLE ---------------------------------------------------------------------------


async def test_a_step_is_declined_unless_the_robot_is_idle() -> None:
    """Steps run in IDLE and nowhere else (SDS §3.9.5): a gearbox running during LISTENING
    feeds itself to the microphone. Before the state machine has said IDLE, nothing moves.

    Neuter: drop the ``_idle`` check in ``perform`` and this rig steps during BOOTING."""
    built, bus = await _make_rig(idle=False)
    try:
        await built.service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await _settle(built.service)
        assert built.drive.runs == []
        assert built.service.steps_declined == 1

        await _go(built, RobotState.LISTENING)
        await built.service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await _settle(built.service)
        assert built.drive.runs == []
        assert built.service.steps_declined == 2
    finally:
        await built.service.stop()
        await bus.stop()


async def test_leaving_idle_preempts_a_step_in_flight_and_stops_the_motors() -> None:
    """A conversation starting mid-step cuts it: the motors stop through the port, the cut is
    reported as ``preempted``, and the odometer keeps the honest partial figure.

    Neuter: remove the ``_preempt`` in ``_on_state_transitioned`` and the leg runs to the end
    with the robot LISTENING."""
    built, bus = await _make_rig(geometry=_LONG)
    try:
        await built.service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await _wait_until_moving(built)

        await _go(built, RobotState.LISTENING)
        # The abort is published only once the cut leg has reported what it drove — a slice of
        # the fake's clock later — so wait for the FACT, not for the handle to clear.
        await built.collector.wait_for(2)  # started + aborted
        await _settle(built.service)

        aborted = built.collector.of(DriveStepAborted)
        assert [(e.gesture, e.reason) for e in aborted] == [("step_toward", PREEMPTED)]  # type: ignore[attr-defined]
        assert built.drive.stops >= 1
        assert not built.drive.is_running
        assert 0 < built.service.offset_mm < _LONG.step_mm, (
            "the cut leg ran to completion"
        )
        assert built.collector.of(DriveStepCompleted) == []
    finally:
        await built.service.stop()
        await bus.stop()


async def test_sleeping_cuts_the_motors_even_with_nothing_running(rig: Rig) -> None:
    """A sleeping robot that rolls is a robot that did not go to sleep — so SLEEPING stops the
    wheels unconditionally, not only when a step is in flight."""
    before = rig.drive.stops
    await _go(rig, RobotState.SLEEPING)
    assert rig.drive.stops == before + 1


# --- the edge: sensors AND budget (AC-5) ----------------------------------------------------


async def test_an_edge_before_the_first_leg_means_no_leg_at_all(rig: Rig) -> None:
    """The pre-leg read. Nothing is driven, the abort names ``edge``, and the latch is set.

    Neuter: remove the ``clear()`` before ``run`` in ``_run_leg`` and the out leg runs onto
    the edge before the poll catches it."""
    rig.edge.clear_flag = False
    await rig.service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
    await rig.collector.wait_for(2)  # started + aborted
    await _settle(rig.service)

    assert rig.drive.runs == []
    aborted = rig.collector.of(DriveStepAborted)
    assert [e.reason for e in aborted] == [EDGE]  # type: ignore[attr-defined]
    assert rig.service.edge_latched
    assert rig.edge.reads >= 1


async def test_an_edge_mid_leg_stops_the_motors_and_the_robot_retreats() -> None:
    """The row the sensors earned their place for (F-13).

    Mid out-leg the desk vanishes: the motors stop **through the port** (``stops`` moves), the
    out run is partial, a retreat run goes *backward* — away from the edge — by the distance the
    service believes it drove, and the abort is reported. Neuter: delete the poll loop's
    ``stop()`` and the out leg completes with the sensor screaming."""
    built, bus = await _make_rig(geometry=_LONG)
    try:
        await built.service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await _wait_until_moving(built)
        stops_before = built.drive.stops

        built.edge.clear_flag = False
        await built.collector.wait_for(2)  # started + aborted
        await _settle(built.service)

        assert built.drive.stops > stops_before
        assert _headings(built) == ["fwd", "back"]
        out, back = built.drive.runs
        assert (
            out[2] < _LONG.step_mm / (_LONG.speed_frac * _CAPS.mm_per_s_at_full) * 1000
        )
        assert built.service.offset_mm == pytest.approx(0.0, abs=1.0)
        aborted = built.collector.of(DriveStepAborted)
        assert [e.reason for e in aborted] == [EDGE]  # type: ignore[attr-defined]
        assert built.service.edge_latched
        assert built.service.steps_aborted[EDGE] == 1
    finally:
        await built.service.stop()
        await bus.stop()


async def test_the_latch_holds_until_clear_has_held_and_then_releases(rig: Rig) -> None:
    """A hand that lifts for a moment is not a desk that came back.

    After an edge abort, a step is declined while ``clear()`` flickers, and accepted only once
    it has held for ``clear_hold_ms`` — polled on the injected clock. Neuter: make
    ``_edge_has_cleared`` return ``True`` on the first clear read and the second ``perform``
    below steps with the sensor still lying."""
    rig.edge.clear_flag = False
    await rig.service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
    await rig.collector.wait_for(2)
    await _settle(rig.service)
    assert rig.service.edge_latched

    # Still an edge: declined, nothing runs.
    await rig.service.perform(Gesture.STEP_BACK, correlation_id=uuid4())
    await _settle(rig.service)
    assert rig.drive.runs == []
    assert rig.service.steps_declined == 1

    # Clear now, and it holds: the hold is polled on the fake clock, so drive it.
    rig.edge.clear_flag = True
    accept = asyncio.create_task(
        rig.service.perform(Gesture.STEP_BACK, correlation_id=uuid4())
    )
    for _ in range(_CLEAR_HOLD_MS // _POLL_MS + 2):
        for _ in range(200):
            if accept.done() or rig.clock.sleepers:
                break
            await asyncio.sleep(0)
        if accept.done():
            break
        # PAST each poll's deadline, never onto it — FakeClock wakes only what an advance crosses.
        await rig.clock.advance(_POLL_MS * 1.5 / 1000)
    await asyncio.wait_for(accept, _ARRIVAL_TIMEOUT_S)
    await rig.collector.wait_for(4)  # + started + completed
    await _settle(rig.service)

    assert not rig.service.edge_latched
    assert _headings(rig) == ["back", "fwd"]


async def test_the_budget_guard_refuses_a_leg_that_would_leave_the_budget(
    rig: Rig,
) -> None:
    """The loud row (``reason="budget"``). Should never fire — every plan is bounded — so it is
    provoked by hand: an offset far beyond the budget, which homing reduces by one budget's
    worth and no more, leaves the out leg unable to run without going further out.

    Neuter: drop the guard in ``_run_leg`` and the out leg runs from 70 mm to 90 mm."""
    rig.service._offset_mm = 100.0  # noqa: SLF001 - provoking a state the planner forbids
    await rig.service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
    await rig.collector.wait_for(2)
    await _settle(rig.service)

    assert _headings(rig) == ["back"], "only the homing leg may have run"
    assert rig.service.offset_mm == pytest.approx(70.0, abs=1.0)
    aborted = rig.collector.of(DriveStepAborted)
    assert [e.reason for e in aborted] == [BUDGET]  # type: ignore[attr-defined]


# --- homing: an abort is a delay, not a drift ------------------------------------------------


async def test_after_an_abort_the_next_step_homes_first() -> None:
    """A step cut short leaves an offset; the next step's first leg undoes it before the plan
    runs, so error does not accumulate over a night of interrupted shuffles.

    Neuter: drop ``homing`` from ``_run`` and the second step starts from wherever the first
    was cut, and the odometer ends off-origin."""
    built, bus = await _make_rig(geometry=_LONG)
    try:
        await built.service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await _wait_until_moving(built)
        await _go(built, RobotState.LISTENING)  # preempt mid out-leg
        await built.collector.wait_for(
            2
        )  # started + aborted: the odometer is settled by then
        await _settle(built.service)
        offset = built.service.offset_mm
        assert offset > 0
        built.drive.runs.clear()

        await _go(built, RobotState.IDLE)
        await built.service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await built.collector.wait_for(4)  # + started + completed
        await _settle(built.service)

        assert _headings(built) == ["back", "fwd", "back"], "no homing leg ran first"
        assert built.service.offset_mm == pytest.approx(0.0, abs=1.0)
        assert built.drive.odometer_mm == pytest.approx(0.0, abs=1.0)
    finally:
        await built.service.stop()
        await bus.stop()


# --- faults and shutdown --------------------------------------------------------------------


class _FaultingDrive(FakeDrive):
    """A drive whose next run raises — §3.12.3's device fault, on cue."""

    def __init__(self) -> None:
        super().__init__(capabilities=_CAPS)
        self.fail_next = False

    async def run(self, left: float, right: float, *, duration_ms: int) -> int:
        if self.fail_next:
            self.fail_next = False
            raise OSError("GPIO write failed")
        return await super().run(left, right, duration_ms=duration_ms)


async def test_a_device_fault_stops_the_motors_and_the_service_stays_live() -> None:
    """*Nothing except a bad API key at boot is allowed to stop the robot.* The fault is
    reported as ``fault``, the motors are stopped, and — the claim that matters — the **next**
    step still runs. "It did not crash" and "it still works" are different claims."""
    drive = _FaultingDrive()
    built, bus = await _make_rig(drive=drive)
    try:
        drive.fail_next = True
        await built.service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await built.collector.wait_for(2)
        await _settle(built.service)

        aborted = built.collector.of(DriveStepAborted)
        assert [e.reason for e in aborted] == [FAULT]  # type: ignore[attr-defined]
        assert drive.stops >= 1
        assert built.collector.of(SystemHandlerFailed) == []

        await built.service.perform(Gesture.STEP_BACK, correlation_id=uuid4())
        await built.collector.wait_for(4)
        await _settle(built.service)
        assert built.collector.of(DriveStepCompleted)
    finally:
        await built.service.stop()
        await bus.stop()


async def test_stop_mid_leg_cuts_the_motors_and_is_idempotent() -> None:
    """The one failure mode that outlives the process: a wheel left turning. ``stop()`` cancels
    the leg and stops the motors unconditionally, twice is fine."""
    built, bus = await _make_rig(geometry=_LONG)
    try:
        await built.service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await _wait_until_moving(built)

        await built.service.stop()
        await built.service.stop()

        assert not built.drive.is_running
        assert built.drive.stops >= 1
        assert built.drive.runs and built.drive.runs[0][2] < 600
    finally:
        await bus.stop()


# --- idle steps: the only trigger in v1 -----------------------------------------------------


async def _advance_past_one_idle_step(rig: Rig) -> None:
    """Past the band's maximum, not to it — ``FakeClock`` wakes only the sleepers an advance
    crosses (the same trap ``test_motion.py`` documents)."""
    await _wait_for_a_sleeper(rig.clock)
    await rig.clock.advance(_IDLE_MAX_S + 1)
    await asyncio.sleep(0)
    await rig.collector.wait_for(len(rig.collector.events) + 2)
    await _settle(rig.service)


async def test_an_idle_robot_eventually_steps_and_alternates(rig: Rig) -> None:
    """Nobody spoke to it and no affect changed; the scheduler alone shifted its weight — first
    toward, then back — and every step was net-zero."""
    await _advance_past_one_idle_step(rig)
    await _advance_past_one_idle_step(rig)

    started = rig.collector.of(DriveStepStarted)
    assert [e.gesture for e in started] == ["step_toward", "step_back"]  # type: ignore[attr-defined]
    assert rig.service.offset_mm == pytest.approx(0.0, abs=1.0)


async def test_the_idle_scheduler_does_not_step_outside_idle() -> None:
    """The band elapses while the robot is LISTENING: nothing moves, and nothing is queued for
    later — a step deferred by a conversation is a step nobody wanted."""
    built, bus = await _make_rig()
    try:
        await _go(built, RobotState.LISTENING)
        await _wait_for_a_sleeper(built.clock)
        await built.clock.advance(_IDLE_MAX_S + 1)
        for _ in range(6):
            await asyncio.sleep(0)
        await _settle(built.service)

        assert built.drive.runs == []
        assert built.collector.events == []
    finally:
        await built.service.stop()
        await bus.stop()


async def test_the_idle_scheduler_does_not_step_while_latched(rig: Rig) -> None:
    """An edge latch outlives the band: the scheduler waits for a step that clears it, rather
    than polling the sensors on its own behalf."""
    rig.edge.clear_flag = False
    await rig.service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
    await rig.collector.wait_for(2)
    await _settle(rig.service)
    events_before = len(rig.collector.events)

    await _wait_for_a_sleeper(rig.clock)
    await rig.clock.advance(_IDLE_MAX_S + 1)
    for _ in range(6):
        await asyncio.sleep(0)
    await _settle(rig.service)

    assert rig.drive.runs == []
    assert len(rig.collector.events) == events_before


# --- the single caller ----------------------------------------------------------------------


def test_it_is_the_only_caller_of_the_drive_and_edge_ports() -> None:
    """SDS §9.1.4 assigns ``Drive.run``/``stop`` and ``EdgeSensor.clear`` to this service, as
    ``Servo.move_to`` belongs to ``MotionService``. A second caller is two schedulers on one
    pair of wheels — and for a stop, two owners of the thing that beats a fall."""
    root = Path(__file__).resolve().parents[2] / "avid"
    callers = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if any(
            needle in path.read_text(encoding="utf-8")
            for needle in ("drive.run(", "drive.stop(", "edge.clear(")
        )
    }
    assert callers == {"services/drive.py"}
