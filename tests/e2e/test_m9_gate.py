"""M9 gate — affect drives gesture, end to end, in milliseconds (#207, SDS §14.5).

The mechanised half of the milestone seal. #207 is the *live* gate — a human, the real rig, an
ear in a quiet room — and this is what makes that run a **confirmation** rather than the first
time the whole chain has ever executed.

The arc, in one test each, matching PMP §5.2's clauses:

* **Affect drives gesture.** ``AffectService`` publishes, ``MotionService`` moves, and neither
  knows the other exists. This is §3.7.2's fan-out — *"the face changes **and** the servo
  nods"* — closed for the first time.
* **Nod and turn, both.** The claim ADR-009's second servo makes, and the one that was
  *unsatisfiable* on the old rig: §2.7.1 used to read "nod **or** turn, not both".
* **Gesture preemption works.** A newer gesture cuts an older one, the trace is partial, and
  ``motion.gesture_preempted`` names the interrupter.
* **Relaxes when idle.** Every channel de-energised after the window, on a fake clock.

Three services and a real bus, wired the way ``main._wire_services`` wires them. What is faked
is the *hardware* — ``FakeServo``, ``FakeClock`` — which is exactly the split §3.9.2 designs
for: the simulator is not a separate program, it is this program with different adapters.

⚠️ **The harness's own pass/fail logic is under test** (CLAUDE.md §7.1, and M4's lesson that *a
gate that can pass on silence is not a gate*). Each criterion has a companion below that
neuters exactly one guard and asserts the criterion goes **red**. A criterion nobody has
watched fail is a criterion nobody has tested.

⚠️ **What this cannot prove, and #207 must.** ``FakeServo`` records a movement trace, and **a
trace is not a moved head.** A wrong channel, a stale one-axis config, a horn slipping on its
spline, an I²C address collision — every one of those produces a perfect trace here and a
motionless robot on the desk. Everything below is necessary and none of it is sufficient.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import NamedTuple
from uuid import uuid4

import pytest

from avid.adapters.clock import FakeClock
from avid.adapters.servo import FakeServo
from avid.core.affect_map import gesture_for
from avid.core.event_bus import AsyncioEventBus
from avid.domain import (
    Affect,
    Axis,
    Event,
    Gesture,
    MotionGestureCompleted,
    MotionGesturePreempted,
    MotionGestureStarted,
    RobotState,
    StateTransitioned,
    Trigger,
    plan,
)
from avid.services.affect import AffectService
from avid.services.motion import MotionService
from avid.services.observability import ObservabilityService

# The rig config/*.toml declares since #200 (ADR-009): pan ch0 body turn, tilt ch13 head, with
# deliberately different reaches. The channels are the numbers #207 has to confirm against
# solder — ch13 is not a typo, it is where the second servo is wired.
_PAN = Axis(name="pan", channel=0, min_deg=30.0, max_deg=150.0)
_TILT = Axis(name="tilt", channel=13, min_deg=60.0, max_deg=120.0)

_IDLE_RELAX_MS = 3000
_TIMEOUT_S = 2.0

# Far longer than any test here: idle drift (#205) is a real feature and a nuisance to a gate
# that is measuring something else, so it is pushed out of the way rather than switched off —
# switching it off would mean this gate runs a robot nobody ships.
_NO_DRIFT = {
    "drift_interval_min_s": 600.0,
    "drift_interval_max_s": 1200.0,
    "drift_amplitude_frac": 0.03,
}


class Rig(NamedTuple):
    """The wired graph, plus the two instruments a criterion is read from."""

    affect: AffectService
    motion: MotionService
    observability: ObservabilityService
    servo: FakeServo
    clock: FakeClock
    bus: AsyncioEventBus
    events: list[Event]


async def _compose(*axes: Axis, idle_relax_ms: int = _IDLE_RELAX_MS) -> Rig:
    """Wire the real services against fake hardware, the way ``main`` wires them.

    ``ObservabilityService`` is included deliberately: §9.1.3 makes it the only subscriber of
    the three ``motion.*`` rows, and #207 reads its log lines to confirm preemption *"by log
    and by eye"*. A gate that omitted it would be testing a graph the robot does not run.
    """
    clock = FakeClock()
    bus = AsyncioEventBus()
    servo = FakeServo(axes=axes)
    affect = AffectService(bus=bus, clock=clock)
    motion = MotionService(
        bus=bus,
        servo=servo,
        clock=clock,
        idle_relax_ms=idle_relax_ms,
        look_at_cooldown_ms=4000,
        **_NO_DRIFT,  # type: ignore[arg-type]
    )
    observability = ObservabilityService()
    events: list[Event] = []

    async def collect(event: Event) -> None:
        events.append(event)

    for service in (affect, motion, observability):
        for sub in service.subscriptions():
            bus.subscribe(
                sub.event_type,
                sub.handler,
                name=sub.name,
                policy=sub.policy,
                maxsize=sub.maxsize,
            )
    for event_type in (
        MotionGestureStarted,
        MotionGestureCompleted,
        MotionGesturePreempted,
    ):
        bus.subscribe(event_type, collect, name=f"gate.{event_type.__name__}")

    await bus.start()
    await motion.start()
    return Rig(affect, motion, observability, servo, clock, bus, events)


async def _teardown(rig: Rig) -> None:
    await rig.motion.stop()
    await rig.bus.stop()


async def _settle(rig: Rig) -> None:
    """Let the bus deliver and the gesture finish. Never a sleep on a guess."""
    for _ in range(4):
        await asyncio.sleep(0)
    task = rig.motion._task  # noqa: SLF001 - the gate reads the service's own handle
    if task is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), _TIMEOUT_S)
    for _ in range(4):
        await asyncio.sleep(0)


async def _feel(rig: Rig, affect: Affect) -> None:
    """Drive the robot's emotional state through the **real** ``AffectService`` surface.

    Not by publishing ``affect.changed`` by hand. §9.1.4 makes ``set_affect`` a direct call and
    the event a *fact* about what happened; a gate that published the fact itself would prove
    the subscriber works and say nothing about whether anything ever publishes one."""
    await rig.affect.set_affect(affect, correlation_id=uuid4())
    await _settle(rig)


def _moves_on(rig: Rig, axis: Axis) -> list[float]:
    return [angle for channel, angle in rig.servo.moves if channel == axis.channel]


def _of(rig: Rig, event_type: type[Event]) -> list[Event]:
    return [event for event in rig.events if isinstance(event, event_type)]


# --- AC-1: affect drives gesture ---------------------------------------------


async def test_happy_nods_on_the_tilt_axis() -> None:
    """The gate's first clause, and the arrow §3.7.2 drew before any of this existed.

    ``AffectService.set_affect(HAPPY)`` publishes one ``affect.changed``; ``MotionService``
    hears it, asks ``gesture_for`` what that means, asks ``plan()`` what a nod is on *this* rig,
    and moves. Nothing in the chain names anything else in it.

    **On the tilt axis** is the load-bearing half. A nod on pan is a horizontal wiggle that
    reads as "no" — the failure ADR-009's second servo exists to end, and the one #207 confirms
    by eye because a trace cannot tell you what a movement looked like."""
    rig = await _compose(_PAN, _TILT)
    try:
        await _feel(rig, Affect.HAPPY)

        assert _moves_on(rig, _TILT), "HAPPY moved nothing on the tilt axis"
        assert _moves_on(rig, _PAN) == [], "the nod leaked onto the pan axis"
        started = _of(rig, MotionGestureStarted)
        assert [e.gesture for e in started] == ["nod"]  # type: ignore[attr-defined]
    finally:
        await _teardown(rig)


async def test_the_gate_fails_if_affect_maps_to_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neuter the map. The criterion must go red.

    If a nod still appeared with ``gesture_for`` returning ``None`` for everything, then
    something other than the affect→gesture policy is driving the servo — and the criterion
    would be measuring the wiring rather than the arrow."""
    monkeypatch.setattr("avid.services.motion.gesture_for", lambda _affect: None)
    rig = await _compose(_PAN, _TILT)
    try:
        await _feel(rig, Affect.HAPPY)

        assert rig.servo.moves == [], (
            "the rig moved with the affect map neutered — this criterion does not measure it"
        )
    finally:
        await _teardown(rig)


async def test_most_affects_move_nothing() -> None:
    """The restraint half of AC-1, and it is not a footnote.

    ``affect.changed`` fires on every state transition. A robot that gestured on each would
    never be idle long enough to relax — failing the gate's fourth clause **by construction** —
    and would put sustained load on the rail #206 is measuring. So a passing gate has to show
    the robot *not* moving as much as it shows it moving."""
    rig = await _compose(_PAN, _TILT)
    try:
        for affect in (Affect.LISTENING, Affect.THINKING, Affect.SPEAKING):
            assert gesture_for(affect) is None, "this test's premise changed"
            await _feel(rig, affect)

        assert rig.servo.moves == []
        assert rig.events == []
    finally:
        await _teardown(rig)


# --- AC-2: nod AND turn, the claim the second servo makes --------------------


async def test_the_rig_can_nod_and_turn() -> None:
    """§2.7.1 used to read *"1 DoF. Gesture vocabulary is nod **or** turn, not both."*

    This is the criterion that proves the hardware change landed end to end, because on the old
    rig it could not be satisfied at all. Both gestures, each on its own axis, in one run."""
    rig = await _compose(_PAN, _TILT)
    try:
        await rig.motion.perform(Gesture.NOD, correlation_id=uuid4())
        await _settle(rig)
        nod_axes = {channel for channel, _ in rig.servo.moves}

        rig.servo.moves.clear()
        await rig.motion.perform(Gesture.TURN_LEFT, correlation_id=uuid4())
        await _settle(rig)
        turn_axes = {channel for channel, _ in rig.servo.moves}

        assert nod_axes == {_TILT.channel}
        assert turn_axes == {_PAN.channel}
        assert nod_axes != turn_axes, (
            "nod and turn used the same axis — this is a 1 DoF rig"
        )
    finally:
        await _teardown(rig)


async def test_the_gate_fails_on_a_one_servo_rig() -> None:
    """Neuter the rig. The 2 DoF criterion must go red.

    A pan-only robot still nods — it degrades to a small sway (#201) — so a criterion that only
    asked *"did it move"* would pass on the hardware ADR-009 replaced. What separates them is
    that nod and turn land on **different** axes, and here they cannot."""
    rig = await _compose(_PAN)
    try:
        await rig.motion.perform(Gesture.NOD, correlation_id=uuid4())
        await _settle(rig)
        nod_axes = {channel for channel, _ in rig.servo.moves}

        rig.servo.moves.clear()
        await rig.motion.perform(Gesture.TURN_LEFT, correlation_id=uuid4())
        await _settle(rig)
        turn_axes = {channel for channel, _ in rig.servo.moves}

        assert nod_axes == turn_axes == {_PAN.channel}
    finally:
        await _teardown(rig)


# --- AC-3: gesture preemption ------------------------------------------------


async def test_a_newer_gesture_cuts_the_one_in_flight_and_says_so() -> None:
    """Three claims, because separately each is satisfiable by a broken robot.

    The old gesture's trace is **partial** — it was genuinely cut, not left to finish. The
    preemption is **reported**, with the interrupter in ``by``. And the new gesture **completes**
    — preempting is not the same as breaking.

    ⚠️ #207 grades this *"by log **and** by eye"*, and the reason is in the middle claim: an
    event firing while the head calmly completes its original sweep is exactly the defect the
    live gate exists to catch, and it looks perfect from here."""
    rig = await _compose(_PAN, _TILT)
    try:
        await rig.motion.perform(Gesture.NOD, correlation_id=uuid4())
        for _ in range(6):  # let a step or two of the sweep land
            await asyncio.sleep(0.01)
            if rig.servo.moves:
                break
        assert rig.servo.moves, "the nod never started, so nothing was preempted"

        await rig.motion.perform(Gesture.TURN_LEFT, correlation_id=uuid4())
        await _settle(rig)

        preempted = _of(rig, MotionGesturePreempted)
        assert [(e.gesture, e.by) for e in preempted] == [("nod", "turn_left")]  # type: ignore[attr-defined]
        assert len(_moves_on(rig, _TILT)) < len(plan(Gesture.NOD, (_PAN, _TILT))), (
            "the preempted nod ran to completion"
        )
        completed = _of(rig, MotionGestureCompleted)
        assert [e.gesture for e in completed] == ["turn_left"]  # type: ignore[attr-defined]
    finally:
        await _teardown(rig)


async def test_the_gate_fails_if_nothing_is_ever_preempted() -> None:
    """Neuter the interruption. The criterion must go red.

    Two gestures performed in sequence, with the second waiting politely for the first: every
    other signal looks the same — both gestures complete, both traces are full — and the only
    difference is the absence of the event. Which is precisely why the criterion asserts the
    event rather than the movement."""
    rig = await _compose(_PAN, _TILT)
    try:
        await rig.motion.perform(Gesture.NOD, correlation_id=uuid4())
        await _settle(rig)  # let it finish first — no overlap, so no preemption
        await rig.motion.perform(Gesture.TURN_LEFT, correlation_id=uuid4())
        await _settle(rig)

        assert _of(rig, MotionGesturePreempted) == []
        assert len(_of(rig, MotionGestureCompleted)) == 2
    finally:
        await _teardown(rig)


# --- AC-4: relaxes when idle -------------------------------------------------


async def test_every_channel_is_de_energised_after_the_idle_window() -> None:
    """The gate's "no buzz" clause, as far as a fake can carry it.

    A held SG90 buzzes audibly and warms, and an idle robot that hums is a robot that gets
    unplugged. ⚠️ What a fake proves is that the *command* was issued; whether the rig is
    actually silent is #207's, with an ear, in a quiet room — the one criterion in this
    milestone that no assertion can reach."""
    rig = await _compose(_PAN, _TILT)
    try:
        await _feel(rig, Affect.HAPPY)
        assert any(rig.servo.is_energised(a.channel) for a in (_PAN, _TILT))

        await rig.clock.advance(_IDLE_RELAX_MS / 1000 + 1)
        await _settle(rig)

        assert not any(rig.servo.is_energised(a.channel) for a in (_PAN, _TILT))
    finally:
        await _teardown(rig)


async def test_the_gate_fails_if_the_clock_never_advances() -> None:
    """Neuter time. "Relaxes when idle" is a *timing* claim.

    A criterion that passed without time passing would be measuring the wiring — or worse, a
    service that relaxes the instant a gesture ends, which would leave the head limp between the
    two halves of a reaction and read as a fault."""
    rig = await _compose(_PAN, _TILT)
    try:
        await _feel(rig, Affect.HAPPY)
        await _settle(rig)  # deliberately no clock advance

        assert any(rig.servo.is_energised(a.channel) for a in (_PAN, _TILT))
    finally:
        await _teardown(rig)


async def test_shutdown_relaxes_every_channel() -> None:
    """The one failure mode that outlives the process.

    Everything else this milestone can get wrong stops when the robot stops. A channel left
    holding torque keeps drawing current and humming after the program is gone, until somebody
    pulls the plug — and on a shared rail that is R-04's territory, not a cosmetic one."""
    rig = await _compose(_PAN, _TILT)
    await _feel(rig, Affect.HAPPY)
    assert any(rig.servo.is_energised(a.channel) for a in (_PAN, _TILT))

    await _teardown(rig)

    assert not any(rig.servo.is_energised(a.channel) for a in (_PAN, _TILT))


# --- the whole arc, and the state feed ---------------------------------------


async def test_sleep_lets_go_of_the_rig_immediately() -> None:
    """Not after ``idle_relax_ms`` — now. A robot that has just fallen asleep is a robot nobody
    is looking at, and humming into an empty room is the one interval where it is most
    obviously wrong."""
    rig = await _compose(_PAN, _TILT)
    try:
        await _feel(rig, Affect.HAPPY)
        assert any(rig.servo.is_energised(a.channel) for a in (_PAN, _TILT))

        await rig.bus.publish(
            StateTransitioned(
                event_id=uuid4(),
                correlation_id=uuid4(),
                timestamp_ms=1,
                monotonic_ns=1,
                source="StateManager",
                from_=RobotState.IDLE,
                to=RobotState.SLEEPING,
                trigger=Trigger.PRESENCE_LOST_TIMEOUT,
            )
        )
        await _settle(rig)

        assert not any(rig.servo.is_energised(a.channel) for a in (_PAN, _TILT))
    finally:
        await _teardown(rig)


async def test_the_motion_events_reach_the_log() -> None:
    """§9.1.3 names ``ObservabilityService`` as the only subscriber of the three ``motion.*``
    rows, and #207's AC-3 grades preemption *by log*.

    So the log is a **gate artefact**, not a debugging convenience: if nothing subscribed, the
    events would publish into an empty room and the live criterion would have nothing to read.
    Asserted through the service's own counter rather than by capturing text, because the
    counter is what proves the subscription is **registered and delivering** — a log assertion
    would pass against a handler nobody ever called.

    The instance is the one ``_compose`` wired, not a fresh one: subscription is **static**, so
    the bus refuses a late ``subscribe`` (SDS §3.5.2), and a second instance would be a
    subscriber the running graph does not have."""
    rig = await _compose(_PAN, _TILT)
    try:
        assert {sub.event_type for sub in rig.observability.subscriptions()} >= {
            MotionGestureStarted,
            MotionGestureCompleted,
            MotionGesturePreempted,
        }, "ObservabilityService stopped subscribing to the motion.* rows"

        await _feel(rig, Affect.HAPPY)
        assert rig.observability.gestures == 1
    finally:
        await _teardown(rig)


async def test_the_whole_arc_in_one_run() -> None:
    """Feel, move, be interrupted, finish, go quiet — the milestone in one sequence.

    The individual criteria above each isolate a property; this one asserts they **compose**,
    which is the thing a live run actually exercises and the thing #158/#161/#162 proved can
    fail while every row looks defensible alone."""
    rig = await _compose(_PAN, _TILT)
    try:
        await _feel(rig, Affect.HAPPY)  # nod, on tilt
        await rig.motion.perform(Gesture.TURN_RIGHT, correlation_id=uuid4())
        await _settle(rig)
        await rig.clock.advance(_IDLE_RELAX_MS / 1000 + 1)
        await _settle(rig)

        assert _moves_on(rig, _TILT), "never nodded"
        assert _moves_on(rig, _PAN), "never turned"
        assert [e.gesture for e in _of(rig, MotionGestureCompleted)] == [  # type: ignore[attr-defined]
            "nod",
            "turn_right",
        ]
        assert not any(rig.servo.is_energised(a.channel) for a in (_PAN, _TILT))
    finally:
        await _teardown(rig)
