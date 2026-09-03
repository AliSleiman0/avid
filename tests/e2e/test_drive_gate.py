"""M12 gate — a bounded, net-zero step, and the hand that stops it, in milliseconds (#400, SDS §14.5).

The mechanised half of the milestone seal. The *live* gate is a person, a desk and an actual
hand under an actual sensor; this is what makes that run a **confirmation** rather than the
first time the whole chain has ever executed. Matching PMP §5.2's M12 clauses:

* **A bounded, net-zero step.** Out, back, and both odometers — the adapter's, integrated from
  what it ran, and the service's, integrated from what the port reported — read zero.
* **A hand under a front sensor stops it mid-leg and it returns.** The motors stop through the
  port, the robot retreats, the abort is reported as ``edge`` and Observability counts it.
* **It never steps outside IDLE.** The scheduler and a direct request both decline.

Two services and a real bus, wired the way ``main._wire_services`` wires them; what is faked is
the *hardware* — ``FakeDrive``, ``FakeEdgeSensor``, ``FakeClock`` — which is exactly the split
§3.9.2 designs for.

⚠️ **The harness's own pass/fail logic is under test** (CLAUDE.md §7.1). Each criterion has a
companion that neuters exactly one guard and asserts the criterion goes **red**. A criterion
nobody has watched fail is a criterion nobody has tested.

⚠️ **What this cannot prove, and the bench must.** An odometer is not a moved robot. A wrong pin,
a sensor wired to the display overlay's GPIO, a polarity that reads a desk as an edge, a loose
5 V connector — every one of those produces a perfect trace here and a robot that either sits
still or does not stop. Everything below is necessary and none of it is sufficient.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import NamedTuple
from uuid import uuid4

import pytest

from avid.adapters.clock import FakeClock
from avid.adapters.drive import FakeDrive
from avid.adapters.edge import FakeEdgeSensor
from avid.core.event_bus import AsyncioEventBus
from avid.core.hal import DriveCapabilities
from avid.domain import (
    EDGE,
    DriveStepAborted,
    DriveStepCompleted,
    Event,
    Gesture,
    RobotState,
    StateTransitioned,
    StepGeometry,
    Trigger,
)
from avid.services.drive import DriveService
from avid.services.observability import ObservabilityService

_TIMEOUT_S = 2.0

# Fast wheels and short legs, so a step is tens of milliseconds of wall clock; a slow variant for
# the criterion that has to interrupt a leg partway. The relations are what the gate grades.
_CAPS = DriveCapabilities(mm_per_s_at_full=1000.0)
_QUICK = StepGeometry(step_mm=20.0, max_excursion_mm=30.0, speed_frac=1.0, dwell_ms=0)
_SLOW = StepGeometry(step_mm=30.0, max_excursion_mm=30.0, speed_frac=0.05, dwell_ms=0)

# Far longer than any test here: the idle scheduler is a real feature and a nuisance to a gate
# measuring something else, so it is pushed out of the way rather than switched off.
_NO_IDLE = {"idle_step_interval_min_s": 600.0, "idle_step_interval_max_s": 1200.0}


class Rig(NamedTuple):
    drive_service: DriveService
    observability: ObservabilityService
    drive: FakeDrive
    edge: FakeEdgeSensor
    clock: FakeClock
    bus: AsyncioEventBus
    events: list[Event]


async def _compose(geometry: StepGeometry = _QUICK) -> Rig:
    """Wire the real services against fake hardware, the way ``main`` wires them.

    ``ObservabilityService`` is included deliberately: §9.1.3 makes it the only subscriber of
    the three ``drive.*`` rows, and the live gate reads the edge abort out of its log.
    """
    clock = FakeClock()
    bus = AsyncioEventBus()
    drive = FakeDrive(capabilities=_CAPS)
    edge = FakeEdgeSensor()
    drive_service = DriveService(
        bus=bus,
        drive=drive,
        edge=edge,
        clock=clock,
        geometry=geometry,
        edge_poll_ms=5,
        edge_clear_hold_ms=50,
        **_NO_IDLE,  # type: ignore[arg-type]
    )
    observability = ObservabilityService()
    events: list[Event] = []

    async def collect(event: Event) -> None:
        events.append(event)

    for service in (drive_service, observability):
        for sub in service.subscriptions():
            bus.subscribe(
                sub.event_type,
                sub.handler,
                name=sub.name,
                policy=sub.policy,
                maxsize=sub.maxsize,
            )
    for event_type in (DriveStepCompleted, DriveStepAborted):
        bus.subscribe(event_type, collect, name=f"gate.{event_type.__name__}")

    await bus.start()
    await drive_service.start()
    rig = Rig(drive_service, observability, drive, edge, clock, bus, events)
    await _go(rig, RobotState.IDLE)
    return rig


async def _teardown(rig: Rig) -> None:
    await rig.drive_service.stop()
    await rig.bus.stop()


async def _go(rig: Rig, state: RobotState) -> None:
    """Drive the state feed the service reads. The gate publishes the fact the state machine
    would publish, because the state machine itself is not under test here."""
    await rig.bus.publish(
        StateTransitioned(
            event_id=uuid4(),
            correlation_id=uuid4(),
            timestamp_ms=1,
            monotonic_ns=1,
            source="StateManager",
            from_=RobotState.IDLE,
            to=state,
            trigger=Trigger.PRESENCE_LOST_TIMEOUT,
        )
    )
    for _ in range(6):
        await asyncio.sleep(0)


async def _settle(rig: Rig, *, events: int) -> None:
    """Wait for the step's FACT to land, then for the task to finish. Never a guessed sleep."""
    for _ in range(2000):
        if len(rig.events) >= events:
            break
        await asyncio.sleep(0.001)
    task = rig.drive_service._task  # noqa: SLF001 - the gate reads the service's own handle
    if task is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), _TIMEOUT_S)
    for _ in range(4):
        await asyncio.sleep(0)


async def _wait_until_moving(rig: Rig) -> None:
    for _ in range(2000):
        if rig.drive.is_running:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("the wheels never started turning")


def _of(rig: Rig, event_type: type[Event]) -> list[Event]:
    return [event for event in rig.events if isinstance(event, event_type)]


# --- AC-1: a bounded, net-zero step -----------------------------------------------------------


async def test_a_step_goes_out_and_comes_back_to_where_it_started() -> None:
    """The milestone's first clause, and ADR-015's whole promise: out, back, zero.

    Both odometers, because they are integrated from different sides of the port and their
    agreement is the claim. The excursion is one leg and the leg is inside the budget."""
    rig = await _compose()
    try:
        await rig.drive_service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await _settle(rig, events=1)

        assert [left > 0 for left, _, _ in rig.drive.runs] == [True, False]
        assert rig.drive.odometer_mm == pytest.approx(0.0, abs=0.5)
        assert rig.drive_service.offset_mm == pytest.approx(0.0, abs=0.5)
        completed = _of(rig, DriveStepCompleted)
        assert [e.net_mm for e in completed] == [0.0]  # type: ignore[attr-defined]
        assert rig.observability.steps == 1
        assert (
            max(abs(rig.drive.odometer_mm), _QUICK.step_mm) <= _QUICK.max_excursion_mm
        )
    finally:
        await _teardown(rig)


async def test_the_gate_fails_if_a_plan_forgets_its_return_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neuter the planner. The criterion must go red.

    A plan that only goes out is what a "step" would be if net-zero were a promise rather
    than a construction — and the odometer is what catches it."""
    from avid.domain import plan_step

    def out_only(*args: object, **kwargs: object) -> tuple[object, ...]:
        legs = plan_step(*args, **kwargs)  # type: ignore[arg-type]
        return legs[:1]

    monkeypatch.setattr("avid.services.drive.plan_step", out_only)
    rig = await _compose()
    try:
        await rig.drive_service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await _settle(rig, events=1)

        assert rig.drive.odometer_mm != pytest.approx(0.0, abs=0.5), (
            "the odometer read zero with the return leg neutered — this criterion does not "
            "measure net-zero"
        )
    finally:
        await _teardown(rig)


# --- AC-2: a hand under the sensor stops it, and it returns ----------------------------------


async def test_a_hand_under_the_front_sensor_stops_a_step_and_the_robot_retreats() -> (
    None
):
    """The row the sensors earned their place for (F-13), and the one the live gate proves with
    an actual hand.

    Three claims, because separately each is satisfiable by a broken robot: the motors were
    stopped **through the port**, the robot **retreated** to where it started, and the abort was
    **reported and counted** as ``edge`` — the log line the live gate reads."""
    rig = await _compose(_SLOW)
    try:
        await rig.drive_service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await _wait_until_moving(rig)
        stops_before = rig.drive.stops

        rig.edge.clear_flag = False  # the hand
        await _settle(rig, events=1)

        assert rig.drive.stops > stops_before, (
            "the motors were never stopped through the port"
        )
        assert [left > 0 for left, _, _ in rig.drive.runs] == [True, False], (
            "no retreat leg ran after the edge"
        )
        assert rig.drive_service.offset_mm == pytest.approx(0.0, abs=1.0)
        aborted = _of(rig, DriveStepAborted)
        assert [e.reason for e in aborted] == [EDGE]  # type: ignore[attr-defined]
        assert rig.observability.steps_aborted[EDGE] == 1
        assert _of(rig, DriveStepCompleted) == [], "a stopped step reported completion"
    finally:
        await _teardown(rig)


async def test_the_gate_fails_if_the_sensors_are_never_consulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neuter the sensor. The criterion must go red.

    A drive that never asks completes the leg with the hand in place, reports a completion,
    and never stops through the port — every assertion above inverts."""

    async def always_clear(self: FakeEdgeSensor) -> bool:
        return True

    monkeypatch.setattr(FakeEdgeSensor, "clear", always_clear)
    rig = await _compose(_SLOW)
    try:
        await rig.drive_service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await _wait_until_moving(rig)
        rig.edge.clear_flag = False
        await _settle(rig, events=1)

        assert _of(rig, DriveStepAborted) == [], (
            "an edge abort was reported with the sensor neutered — this criterion does not "
            "measure the sensors"
        )
        assert _of(rig, DriveStepCompleted), "the step did not even complete"
    finally:
        await _teardown(rig)


# --- AC-3: never outside IDLE -----------------------------------------------------------------


async def test_a_step_is_refused_outside_idle_and_cut_when_idle_ends() -> None:
    """Steps run in IDLE and nowhere else (SDS §3.9.5): a gearbox running during LISTENING feeds
    itself to the microphone, and one running during SPEAKING walks off mid-sentence."""
    rig = await _compose(_SLOW)
    try:
        await _go(rig, RobotState.LISTENING)
        await rig.drive_service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await _settle(rig, events=0)
        assert rig.drive.runs == [], "the robot stepped while LISTENING"

        await _go(rig, RobotState.IDLE)
        await rig.drive_service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await _wait_until_moving(rig)
        await _go(rig, RobotState.SPEAKING)
        await _settle(rig, events=1)

        assert not rig.drive.is_running, "the step kept running after IDLE ended"
        assert _of(rig, DriveStepAborted), "the cut step was not reported"
    finally:
        await _teardown(rig)


async def test_the_gate_fails_if_the_service_stops_reading_the_state_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neuter the state feed. The criterion must go red.

    A service that never learns it left IDLE steps during LISTENING — which is exactly what a
    dropped subscription would look like, and why the drift check names this edge."""

    async def deaf(self: DriveService, event: StateTransitioned) -> None:
        self._idle = True  # noqa: SLF001 - the neuter

    monkeypatch.setattr(DriveService, "_on_state_transitioned", deaf)
    rig = await _compose()
    try:
        await _go(rig, RobotState.LISTENING)
        await rig.drive_service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await _settle(rig, events=1)

        assert rig.drive.runs, (
            "the robot stayed still with the state feed neutered — this criterion does not "
            "measure the IDLE rule"
        )
    finally:
        await _teardown(rig)
