"""``MotionService`` — the affect→gesture path and preemption (#203, SDS §3.6.1, §3.7.2).

Real bus, real ``FakeServo``, real ``FakeClock``; no mocks (SDS §14.3). Waits are on
``asyncio.Event``, never on a sleep — the bus **cancels** its subscriber workers on ``stop()``
rather than draining them, so "publish then sleep a bit" is a race that passes locally and
fails on a loaded CI box.

⚠️ **Gesture durations here are milliseconds, deliberately.** ``FakeServo.move_to`` sleeps on
``asyncio.sleep``, not on the injected ``Clock`` — it is a device fake, and a real servo's
sweep is not fakeable time. So a test that drives a real gesture burns real wall clock, and the
plans below are kept short enough that the suite does not. Only the *relax timer* (#203's
second half) sleeps on the ``Clock`` and is fully fakeable.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import NamedTuple
from uuid import UUID, uuid4

import pytest

from avid.adapters.clock import FakeClock, SystemClock
from avid.adapters.servo import FakeServo
from avid.core.event_bus import AsyncioEventBus
from avid.domain import (
    Affect,
    AffectChanged,
    Axis,
    Direction,
    Event,
    Gesture,
    LookAtResult,
    MotionGestureCompleted,
    MotionGesturePreempted,
    MotionGestureStarted,
    RobotState,
    StateTransitioned,
    SystemHandlerFailed,
    Trigger,
    plan,
)
from avid.services.motion import MotionService

_ARRIVAL_TIMEOUT_S = 2.0

# Gesture sweeps run this many times faster than the plan says. See ``_TracingServo``: a real
# nod is 880 ms of genuine ``asyncio.sleep`` and there are a dozen gestures in this file.
# 5x keeps every leg at two or more steps, so a sweep is still interruptible partway.
_SPEEDUP = 5

# Long enough that no test relaxes by accident between a gesture and its assertion, and short
# enough that a test which *wants* the countdown can drive it with a fake clock in one step.
# The shipped default is 3000 ms (config/*.toml); the number is not what is under test here,
# the arming and cancelling are.
_IDLE_RELAX_MS = 3000

# The look_at rate limit (#204). Long enough that a second call in the same test is refused
# unless the clock is deliberately advanced, so a test cannot pass by accident of timing.
_LOOK_AT_COOLDOWN_MS = 4000

# The rig config/*.toml declares (#200): pan ch0, tilt ch13, asymmetric reaches.
_PAN = Axis(name="pan", channel=0, min_deg=30.0, max_deg=150.0)
_TILT = Axis(name="tilt", channel=13, min_deg=60.0, max_deg=120.0)


class _StepTrace(list[tuple[int, float]]):
    """``FakeServo.moves``, plus a signal so a test can wait for *physical* motion.

    ``gesture_started`` is published **before** the first ``move_to``, so waiting on the event
    proves the gesture was armed and nothing more. Preemption has to be tested against a sweep
    that is genuinely underway — otherwise "the trace is partial" is trivially true of a trace
    that is empty, which would pass while proving the opposite of the intended claim.

    A subclass of the shipped fake's own list rather than a second fake: the same reason
    ``tests/services/test_expression.py`` subclasses ``FakeDisplay`` to add ``wait_for_frames``
    — instrument the real thing, never write a parallel one that can drift from it.
    """

    def __init__(self) -> None:
        super().__init__()
        self._stepped = asyncio.Event()

    def append(self, item: tuple[int, float]) -> None:
        super().append(item)
        self._stepped.set()

    async def wait_for_steps(self, count: int) -> None:
        async with asyncio.timeout(_ARRIVAL_TIMEOUT_S):
            while len(self) < count:
                self._stepped.clear()
                if len(self) >= count:
                    return
                await self._stepped.wait()


class _TracingServo(FakeServo):
    """``FakeServo`` with the signalling trace above, and a scaled clock for the sweep.

    ⚠️ **The scaling is a test-suite cost control, and it is the only thing here that is not
    the shipped behaviour.** ``FakeServo.move_to`` sleeps on ``asyncio.sleep`` because a servo
    sweep is not fakeable time, so a suite that ran every gesture at its real length would add
    ~16 s to every CI run forever — for gestures whose *duration* is the domain's business
    (#201) and not this service's.

    What is preserved is what these tests actually assert: the sweep still crosses multiple
    awaits, so it is still genuinely interruptible partway, and the trace is still a real
    record of commanded steps. Mid-sweep cancellation at full length is the **adapter's**
    contract to keep, and ``tests/contract/test_servo.py::test_move_is_cancellable`` keeps it
    unscaled. ``speedup=1`` is available and used by the one test that measures elapsed time,
    where scaling would be measuring the scaling.
    """

    def __init__(self, *, axes: tuple[Axis, ...], speedup: int = _SPEEDUP) -> None:
        super().__init__(axes=axes)
        self.moves: _StepTrace = _StepTrace()
        self._speedup = speedup

    async def move_to(
        self, channel: int, angle_deg: float, *, duration_ms: int
    ) -> None:
        await super().move_to(
            channel, angle_deg, duration_ms=max(1, duration_ms // self._speedup)
        )


class _Collector:
    """Records every event of the types it is registered for."""

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
    """Everything a test needs, wired the way ``main._wire_services`` wires it."""

    service: MotionService
    bus: AsyncioEventBus
    servo: _TracingServo
    clock: FakeClock
    collector: _Collector


def _register(bus: AsyncioEventBus, service: MotionService) -> None:
    """Register what the service *declared* — the composition root's job (SDS §9.2), kept here
    so this suite stays independent of ``main._wire_services``."""
    for sub in service.subscriptions():
        bus.subscribe(
            sub.event_type,
            sub.handler,
            name=sub.name,
            policy=sub.policy,
            maxsize=sub.maxsize,
        )


async def _make_rig(*axes: Axis) -> tuple[Rig, AsyncioEventBus]:
    clock = FakeClock()
    bus = AsyncioEventBus()
    servo = _TracingServo(axes=axes)
    service = MotionService(
        bus=bus,
        servo=servo,
        clock=clock,
        idle_relax_ms=_IDLE_RELAX_MS,
        look_at_cooldown_ms=_LOOK_AT_COOLDOWN_MS,
    )
    collector = _Collector()

    _register(bus, service)
    # Every motion.* fact plus the bus's own failure fact, so "it published nothing" is a claim
    # about the bus rather than about the module's source text.
    for event_type in (
        MotionGestureStarted,
        MotionGestureCompleted,
        MotionGesturePreempted,
        SystemHandlerFailed,
    ):
        bus.subscribe(event_type, collector.handle, name=f"test.{event_type.__name__}")

    await bus.start()
    await service.start()
    return Rig(service, bus, servo, clock, collector), bus


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    """The 2 DoF rig ADR-009 accepted — the one the robot actually has."""
    built, bus = await _make_rig(_PAN, _TILT)
    try:
        yield built
    finally:
        await built.service.stop()
        await bus.stop()


@pytest.fixture
async def pan_only() -> AsyncIterator[Rig]:
    """The 1-servo fallback ADR-009 retained as a capability path.

    A fallback nobody exercises is a fallback nobody has, and this is the rig every degraded
    branch in ``plan()`` is written for."""
    built, bus = await _make_rig(_PAN)
    try:
        yield built
    finally:
        await built.service.stop()
        await bus.stop()


def _affect_changed(
    affect: Affect, *, monotonic_ns: int = 1, corr: UUID | None = None
) -> AffectChanged:
    """An ``affect.changed`` built by hand, so a test controls ``monotonic_ns`` for staleness."""
    return AffectChanged(
        event_id=uuid4(),
        correlation_id=corr or uuid4(),
        timestamp_ms=1,
        monotonic_ns=monotonic_ns,
        source="AffectService",
        affect=affect,
        tier=2,
        previous=Affect.IDLE,
    )


def _transitioned(*, to: RobotState) -> StateTransitioned:
    """A ``state.transitioned`` built by hand — the same shape ``test_expression.py`` uses, and
    for the same reason: this service reads the state feed independently of whoever drives the
    machine, so a test drives the feed rather than the machine."""
    return StateTransitioned(
        event_id=uuid4(),
        correlation_id=uuid4(),
        timestamp_ms=1,
        monotonic_ns=1,
        source="StateManager",
        from_=RobotState.IDLE,
        to=to,
        trigger=Trigger.PRESENCE_LOST_TIMEOUT,
    )


async def _settle(service: MotionService) -> None:
    """Await the gesture in flight, if any.

    Never a sleep: the task *is* the thing to wait on, and a sleep long enough to be safe is a
    sleep long enough to make the suite slow on the machine where it is least reliable.
    ``shield`` so a cancelled gesture (the preemption tests) is a normal outcome here rather
    than a failure of the test's own waiting.
    """
    task = service._task
    if task is None:
        return
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(task), _ARRIVAL_TIMEOUT_S)


# --- the §3.7.2 arrow, end to end --------------------------------------------


async def test_happy_moves_the_tilt_axis_and_reports_it(rig: Rig) -> None:
    """The gate's first clause: **affect drives gesture**, on the axis that makes it a nod.

    Asserted on the *trace* rather than on a call count — "the head ended up here" is a claim
    about the robot; "``move_to`` was called" is a claim about the code (SDS §14.3). And the
    channel matters: ch13 is tilt. A nod on ch0 would be a pan wiggle, which is what the rig
    did before ADR-009 and what §2.7.1 used to call the constraint."""
    await rig.bus.publish(_affect_changed(Affect.HAPPY))
    await rig.collector.wait_for(2)  # started + completed
    await _settle(rig.service)

    assert {channel for channel, _ in rig.servo.moves} == {_TILT.channel}
    started = rig.collector.of(MotionGestureStarted)
    assert [e.gesture for e in started] == ["nod"]  # type: ignore[attr-defined]
    assert [axis.name for axis in started[0].axes] == ["tilt"]  # type: ignore[attr-defined]


async def test_the_same_affect_degrades_to_pan_on_a_one_servo_rig(
    pan_only: Rig,
) -> None:
    """§3.9.3's negotiation, through the whole stack rather than in the planner alone.

    The service asks the *adapter* what axes exist — never a config list — so the identical
    affect produces a tilt nod on one rig and a small pan sway on another, with no branch
    anywhere in this service."""
    await pan_only.bus.publish(_affect_changed(Affect.HAPPY))
    await pan_only.collector.wait_for(2)
    await _settle(pan_only.service)

    assert {channel for channel, _ in pan_only.servo.moves} == {_PAN.channel}
    started = pan_only.collector.of(MotionGestureStarted)
    assert [axis.name for axis in started[0].axes] == ["pan"]  # type: ignore[attr-defined]


async def test_an_affect_that_maps_to_nothing_moves_nothing(rig: Rig) -> None:
    """**``None`` is the common answer** (#202): the Tier-1 baselines fire on every state
    transition, and a robot that moved on each would never be idle long enough to relax."""
    await rig.bus.publish(_affect_changed(Affect.LISTENING))
    await asyncio.sleep(
        0
    )  # let the handler run; nothing to wait *for* is the assertion
    await _settle(rig.service)

    assert rig.servo.moves == []
    assert rig.collector.events == []


async def test_a_gesture_the_rig_cannot_express_is_a_silent_no_op(
    pan_only: Rig,
) -> None:
    """An empty plan is a legitimate answer, not an error (#201 AC-5).

    ⚠️ And **no ``gesture_started``** — a started event for a gesture that never started would
    make the log claim motion that did not happen, which is the one thing #207 grades by eye."""
    await pan_only.service.perform(Gesture.LOOK_UP, correlation_id=uuid4())
    # ⚠️ Settle before asserting. `perform` ARMS a task and returns, so an assertion made
    # straight afterwards passes even against a service that publishes eagerly — the task
    # simply has not run yet. Found by neutering the no-op and watching this test stay green.
    await _settle(pan_only.service)
    await asyncio.sleep(0)

    assert pan_only.servo.moves == []
    assert pan_only.collector.of(MotionGestureStarted) == []
    assert pan_only.service.gestures_performed == 0


async def test_a_completed_gesture_reports_a_measured_duration() -> None:
    """``duration_ms`` is **measured**, not copied from the plan (AC-4).

    ⚠️ **This is the one test that needs a real clock, and the reason is worth writing down.**
    ``FakeServo.move_to`` sleeps on ``asyncio.sleep`` — it is a device fake, and a servo sweep
    is not fakeable time — while ``FakeClock.monotonic_ns`` is frozen. Together they report a
    perfectly measured **zero**, which would pass a ``>= 0`` assertion and prove nothing. So
    this one wires ``SystemClock``, where real elapsed time and the clock agree.

    Measured with ``monotonic_ns``, never ``timestamp_ms`` (§9.1.1): a wall-clock step yields a
    negative duration and poisons the metric this milestone is graded on. Bounded rather than
    pinned — a pinned figure would make this a stopwatch on the CI runner — but bounded on both
    sides, because "greater than zero" alone would also accept a service that reported the
    plan's own arithmetic."""
    bus = AsyncioEventBus()
    servo = _TracingServo(axes=(_PAN, _TILT), speedup=1)  # measuring elapsed time
    service = MotionService(
        bus=bus,
        servo=servo,
        clock=SystemClock(),
        idle_relax_ms=_IDLE_RELAX_MS,
        look_at_cooldown_ms=_LOOK_AT_COOLDOWN_MS,
    )
    collector = _Collector()
    bus.subscribe(MotionGestureCompleted, collector.handle, name="test.completed")
    await bus.start()
    try:
        await service.perform(Gesture.CENTER, correlation_id=uuid4())
        await _settle(service)
        await collector.wait_for(1)
    finally:
        await service.stop()
        await bus.stop()

    completed = collector.of(MotionGestureCompleted)
    planned = sum(frame.duration_ms for frame in plan(Gesture.CENTER, servo.axes))
    assert 0 < completed[0].duration_ms < planned * 5  # type: ignore[attr-defined]


# --- one gesture at a time ---------------------------------------------------


async def test_a_new_gesture_preempts_the_one_in_flight(rig: Rig) -> None:
    """The gate's fourth clause: **gesture preemption works**.

    Three claims in one, because separately each is satisfiable by a broken service: the old
    gesture's trace is **partial** (it really was cut, not left to finish), the preemption is
    **reported with the interrupting gesture in ``by``** (#207 AC-3 grades exactly this line),
    and the new gesture **completes** (preempting is not the same as breaking)."""
    corr = uuid4()
    await rig.service.perform(Gesture.NOD, correlation_id=corr)
    # Wait for real motion, not merely for the event: gesture_started is published *before*
    # the first move_to, so cutting at that point would leave an empty trace and make "the
    # trace is partial" trivially true.
    await rig.servo.moves.wait_for_steps(1)
    cut_at = len(rig.servo.moves)

    await rig.service.perform(Gesture.TURN_LEFT, correlation_id=corr)
    await _settle(rig.service)
    await rig.collector.wait_for(4)  # started, preempted, started, completed

    preempted = rig.collector.of(MotionGesturePreempted)
    assert [(e.gesture, e.by) for e in preempted] == [("nod", "turn_left")]  # type: ignore[attr-defined]
    tilt_moves = [m for m in rig.servo.moves if m[0] == _TILT.channel]
    assert len(tilt_moves) < len(plan(Gesture.NOD, rig.servo.axes)), (
        "the preempted nod ran to completion — it was not actually cancelled"
    )
    assert cut_at > 0
    completed = rig.collector.of(MotionGestureCompleted)
    assert [e.gesture for e in completed] == ["turn_left"]  # type: ignore[attr-defined]


async def test_the_preemption_is_reported_before_the_new_gesture_starts(
    rig: Rig,
) -> None:
    """Ordering, asserted because it is what makes the log readable at 1 a.m.

    ``perform`` awaits the ``gesture_preempted`` publish rather than spawning it, so a log can
    never read as though the new gesture began before the old one was cut. Spawning would have
    been the obvious shortcut and would have produced exactly that confusion — in the one place
    #207 asks a human to reconcile a log against a moving head."""
    await rig.service.perform(Gesture.NOD, correlation_id=uuid4())
    await rig.servo.moves.wait_for_steps(1)
    await rig.service.perform(Gesture.SHAKE, correlation_id=uuid4())
    await _settle(rig.service)
    await rig.collector.wait_for(4)

    assert [type(event).__name__ for event in rig.collector.events] == [
        "MotionGestureStarted",
        "MotionGesturePreempted",
        "MotionGestureStarted",
        "MotionGestureCompleted",
    ]


async def test_nothing_is_preempted_when_nothing_is_running(rig: Rig) -> None:
    """A first gesture is not an interruption.

    A service that published ``gesture_preempted`` on every ``perform`` would satisfy the
    preemption test above and fill the log with a fact that never happened — and #207 reads
    that log to decide whether preemption works."""
    await rig.service.perform(Gesture.NOD, correlation_id=uuid4())
    await _settle(rig.service)

    assert rig.collector.of(MotionGesturePreempted) == []


async def test_a_completed_gesture_is_not_preempted_by_the_next_one(rig: Rig) -> None:
    """Finishing and being cut short are different facts.

    The in-flight handle survives a completed gesture (the task object is still there, merely
    done), so a naive check would report every second gesture as an interruption of the
    first."""
    await rig.service.perform(Gesture.CENTER, correlation_id=uuid4())
    await _settle(rig.service)
    await rig.service.perform(Gesture.CENTER, correlation_id=uuid4())
    await _settle(rig.service)

    assert rig.collector.of(MotionGesturePreempted) == []
    assert rig.service.gestures_performed == 2


# --- lifecycle ---------------------------------------------------------------


async def test_stop_relaxes_every_channel(rig: Rig) -> None:
    """⚠️ The one failure mode that outlives the process.

    Everything else this service can get wrong stops when the robot stops. A channel left
    holding torque keeps drawing current and humming after the program is gone, until somebody
    pulls the plug — and #207 grades "no buzz" with an ear in a quiet room."""
    await rig.service.perform(Gesture.CENTER, correlation_id=uuid4())
    await _settle(rig.service)
    assert any(rig.servo.is_energised(axis.channel) for axis in rig.servo.axes)

    await rig.service.stop()

    assert not any(rig.servo.is_energised(axis.channel) for axis in rig.servo.axes)


async def test_stop_cancels_a_gesture_in_flight_and_still_relaxes(rig: Rig) -> None:
    """A shutdown mid-nod must not leave the head energised at an arbitrary angle.

    This is the ordering that matters: cancel *then* relax. Relaxing first would be undone by
    the sweep's next step, which is the same trap ``Pca9685Servo``'s docstring warns the caller
    about (#289) — the lock makes each operation atomic, it does not order them."""
    await rig.service.perform(Gesture.NOD, correlation_id=uuid4())
    await rig.servo.moves.wait_for_steps(1)

    await rig.service.stop()

    assert not any(rig.servo.is_energised(axis.channel) for axis in rig.servo.axes)


async def test_stop_is_idempotent(rig: Rig) -> None:
    """§9.2 requires it, and ``lifecycle.run`` can reach it twice on a fault path."""
    await rig.service.stop()
    await rig.service.stop()


async def test_start_moves_nothing(rig: Rig) -> None:
    """A robot that gestured on boot would move before it had anything to express.

    The first thing anyone should see a servo do is nothing — and a boot-time twitch is
    indistinguishable, to a person, from a fault."""
    assert rig.servo.moves == []


# --- the guards ---------------------------------------------------------------


async def test_a_superseded_affect_is_skipped(rig: Rig) -> None:
    """The staleness guard ``ExpressionService`` also carries, and for a sharper reason here.

    The bus guarantees FIFO *per subscriber*, not across them (SDS §3.5), so an older event can
    be handled after a newer one. A late *face* is wrong for a moment; a late *gesture* is a
    servo moving for no reason anyone watching can account for."""
    await rig.bus.publish(_affect_changed(Affect.HAPPY, monotonic_ns=100))
    await rig.collector.wait_for(2)
    await _settle(rig.service)
    before = len(rig.servo.moves)

    await rig.bus.publish(_affect_changed(Affect.SAD, monotonic_ns=50))
    await asyncio.sleep(0)
    await _settle(rig.service)

    assert rig.service.stale_skipped == 1
    assert len(rig.servo.moves) == before


def test_it_is_the_only_caller_of_the_servo_port() -> None:
    """SDS §9.1.4 assigns ``Servo.move_to`` to this service, exactly as ``Display.render``
    belongs to ``ExpressionService``.

    Asserted over the source tree rather than by inspection: a second caller appearing in a
    service is the kind of change that looks harmless in a diff and quietly puts two schedulers
    on one rig."""
    root = Path(__file__).resolve().parents[2] / "avid"
    callers = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if ".move_to(" in path.read_text(encoding="utf-8")
        or ".relax(" in path.read_text(encoding="utf-8")
    }
    # Just the one. The adapters *define* move_to/relax but never call them, and main.py
    # builds the Servo and hands it over without touching it — the `_ = servo` line that
    # stood there for four milestones is gone as of this issue.
    assert callers == {"services/motion.py"}


async def test_a_raising_subscriber_does_not_disturb_a_gesture() -> None:
    """The single most important reliability property in the system, applied here (AC-9).

    A crashing observer must not stop the head. The bus logs, swallows, and republishes
    ``system.handler_failed`` — so the gesture completes *and* the failure is visible, which is
    the pair of properties that makes the bus safe to publish into at all. A crashing display
    renderer must not kill a conversation; a crashing gesture logger must not freeze the head.

    Wired by hand rather than through the ``rig`` fixture because subscription is **static**:
    the bus freezes its subscriber graph at ``start()`` and refuses a later ``subscribe``
    (SDS §3.5.2). That refusal is the feature — a runtime-mutable graph forfeits §9.1.5's drift
    check — so the boom subscriber has to exist before the bus does anything."""

    async def boom(event: Event) -> None:
        raise RuntimeError("subscriber went bang")

    bus = AsyncioEventBus()
    servo = _TracingServo(axes=(_PAN, _TILT))
    service = MotionService(
        bus=bus,
        servo=servo,
        clock=FakeClock(),
        idle_relax_ms=_IDLE_RELAX_MS,
        look_at_cooldown_ms=_LOOK_AT_COOLDOWN_MS,
    )
    collector = _Collector()
    bus.subscribe(MotionGestureStarted, boom, name="test.boom")
    for event_type in (MotionGestureCompleted, SystemHandlerFailed):
        bus.subscribe(event_type, collector.handle, name=f"test.{event_type.__name__}")
    await bus.start()
    try:
        await service.perform(Gesture.CENTER, correlation_id=uuid4())
        await _settle(service)
        await collector.wait_for(2)  # completed + handler_failed
    finally:
        await service.stop()
        await bus.stop()

    assert collector.of(MotionGestureCompleted)
    assert collector.of(SystemHandlerFailed)


async def test_the_correlation_id_travels_with_the_gesture(rig: Rig) -> None:
    """§3.12.2: one grep on a correlation id reconstructs the whole turn.

    The id is **propagated, never minted** here — a gesture is downstream of whatever caused
    the affect, and an event carrying a fresh id would be a fact with no way back to the
    conversation that produced it."""
    corr = uuid4()
    await rig.bus.publish(_affect_changed(Affect.HAPPY, corr=corr))
    await rig.collector.wait_for(2)
    await _settle(rig.service)

    assert {event.correlation_id for event in rig.collector.events} == {corr}


async def test_a_failing_relax_does_not_stop_the_rest_from_relaxing() -> None:
    """⚠️ On a teardown path, a raising ``relax`` must not mask the reason we were relaxing.

    This is the shape that turns one dead channel into a rig full of energised ones: the loop
    aborts on the first exception and every axis after it stays held, humming, after the
    process is gone. #203b makes it reachable for real — the I²C-fault path relaxes *every*
    channel precisely when the bus is already misbehaving, so the one call most likely to
    raise is the one being made to recover from a raise.

    The failure is logged rather than swallowed silently: a relax that did not take is a fact
    about the hardware, and the ear in #207's quiet room is the only other instrument for it.
    """

    class _SulkyServo(_TracingServo):
        async def relax(self, channel: int) -> None:
            if channel == _PAN.channel:
                raise OSError("i2c went away")
            await super().relax(channel)

    bus = AsyncioEventBus()
    servo = _SulkyServo(axes=(_PAN, _TILT))
    service = MotionService(
        bus=bus,
        servo=servo,
        clock=FakeClock(),
        idle_relax_ms=_IDLE_RELAX_MS,
        look_at_cooldown_ms=_LOOK_AT_COOLDOWN_MS,
    )
    await bus.start()
    try:
        await service.perform(Gesture.CENTER, correlation_id=uuid4())
        await _settle(service)
        assert servo.is_energised(_TILT.channel)

        await service.stop()  # must not raise, and must keep going past the bad channel
    finally:
        await bus.stop()

    assert not servo.is_energised(_TILT.channel), (
        "a failing relax on pan stopped tilt from being relaxed at all"
    )


# --- the I²C-fault path (SDS §3.12.3) ----------------------------------------


class _FaultyServo(_TracingServo):
    """Raises on the *n*-th ``move_to``, the way a NACK arrives mid-gesture."""

    def __init__(self, *, axes: tuple[Axis, ...], fail_on: int = 1) -> None:
        super().__init__(axes=axes)
        self._fail_on = fail_on
        self.calls = 0
        self.armed = True

    async def move_to(
        self, channel: int, angle_deg: float, *, duration_ms: int
    ) -> None:
        self.calls += 1
        if self.armed and self.calls >= self._fail_on:
            raise OSError("[Errno 121] Remote I/O error")
        await super().move_to(channel, angle_deg, duration_ms=duration_ms)


async def _faulty_rig(*, fail_on: int = 1) -> tuple[Rig, AsyncioEventBus]:
    clock = FakeClock()
    bus = AsyncioEventBus()
    servo = _FaultyServo(axes=(_PAN, _TILT), fail_on=fail_on)
    service = MotionService(
        bus=bus,
        servo=servo,
        clock=clock,
        idle_relax_ms=_IDLE_RELAX_MS,
        look_at_cooldown_ms=_LOOK_AT_COOLDOWN_MS,
    )
    collector = _Collector()
    _register(bus, service)
    for event_type in (
        MotionGestureStarted,
        MotionGestureCompleted,
        MotionGesturePreempted,
    ):
        bus.subscribe(event_type, collector.handle, name=f"test.{event_type.__name__}")
    await bus.start()
    return Rig(service, bus, servo, clock, collector), bus


async def test_a_device_fault_aborts_relaxes_and_reports_by_none() -> None:
    """§3.12.3 states this as a **fact**, not a suggestion: *"Abort gesture, relax servo,
    publish ``motion.gesture_preempted``, continue."*

    ``by=None`` is what distinguishes a fault from an ordinary interruption on the shared
    catalog row (§9.1.3), and it is the field a subscriber watching for hardware trouble
    filters on — so it is asserted here rather than left to the log."""
    rig, bus = await _faulty_rig()
    try:
        await rig.service.perform(Gesture.NOD, correlation_id=uuid4())
        await _settle(rig.service)
        await rig.collector.wait_for(2)  # started, then preempted-by-fault

        preempted = rig.collector.of(MotionGesturePreempted)
        assert [(e.gesture, e.by) for e in preempted] == [("nod", None)]  # type: ignore[attr-defined]
        assert rig.collector.of(MotionGestureCompleted) == []
        assert rig.service.gestures_aborted == 1
    finally:
        await rig.service.stop()
        await bus.stop()


async def test_a_fault_relaxes_every_channel_not_just_the_one_that_failed() -> None:
    """⚠️ The decision #203 asks to be made deliberately, made in favour of all of them.

    Two arguments, both pointing the same way. A half-completed gesture leaves the head at an
    **arbitrary** angle rather than a resting one — so the axis that did *not* fault is
    precisely the one holding torque somewhere unintended. And a rig that has just failed an
    I²C write is not a rig anyone should trust to keep holding torque at all.

    The cost of being wrong in this direction is a robot that briefly goes limp. In the other
    it is a stalled servo on a browning-out rail — R-04, the risk this milestone has a whole
    spike for."""
    rig, bus = await _faulty_rig(fail_on=2)
    try:
        # CENTER touches both channels, so the first move energises one before the next faults.
        await rig.service.perform(Gesture.CENTER, correlation_id=uuid4())
        await _settle(rig.service)
        await rig.collector.wait_for(2)

        assert not any(
            rig.servo.is_energised(axis.channel) for axis in rig.servo.axes
        ), "a channel was left holding torque after an I2C fault"
    finally:
        await rig.service.stop()
        await bus.stop()


async def test_the_service_survives_a_fault_and_performs_the_next_gesture() -> None:
    """*"Nothing except a bad API key at boot is allowed to stop the robot"* (§3.12.3).

    **"It did not crash" and "it still works" are different claims**, and only the second is
    worth having: a service whose gesture task died quietly would satisfy the first for as long
    as nobody asked it to move again. So this asks it to move again."""
    rig, bus = await _faulty_rig()
    try:
        await rig.service.perform(Gesture.NOD, correlation_id=uuid4())
        await _settle(rig.service)
        await rig.collector.wait_for(2)

        rig.servo.armed = False  # type: ignore[attr-defined]  # the bus recovers
        await rig.service.perform(Gesture.CENTER, correlation_id=uuid4())
        await _settle(rig.service)

        assert rig.service.gestures_performed == 1
        assert rig.service.gestures_aborted == 1
    finally:
        await rig.service.stop()
        await bus.stop()


# --- relaxing when idle — the gate's "no buzz" clause ------------------------


async def test_the_rig_relaxes_after_the_idle_window(rig: Rig) -> None:
    """The gate's fourth clause, driven by a fake clock rather than by waiting three seconds.

    That the countdown sleeps on the injected ``Clock`` is the whole reason this is a
    millisecond test — the same argument §9.3 makes for ``Clock`` being a port at all: the
    alternative is a suite nobody runs, and a "no buzz" claim nobody checks."""
    await rig.service.perform(Gesture.CENTER, correlation_id=uuid4())
    await _settle(rig.service)
    assert any(rig.servo.is_energised(axis.channel) for axis in rig.servo.axes)

    await rig.clock.advance(_IDLE_RELAX_MS / 1000)
    await asyncio.sleep(0)

    assert not any(rig.servo.is_energised(axis.channel) for axis in rig.servo.axes)


async def test_the_rig_stays_energised_before_the_window_elapses(rig: Rig) -> None:
    """The other half, and the one that makes the test above mean something.

    A service that relaxed the instant a gesture finished would pass "relaxes when idle" and
    fail the robot: the head would go limp between the two halves of a reaction, which reads as
    a fault rather than as restraint."""
    await rig.service.perform(Gesture.CENTER, correlation_id=uuid4())
    await _settle(rig.service)

    await rig.clock.advance(_IDLE_RELAX_MS / 1000 / 2)
    await asyncio.sleep(0)

    assert any(rig.servo.is_energised(axis.channel) for axis in rig.servo.axes)


async def test_a_new_gesture_cancels_a_pending_relax(rig: Rig) -> None:
    """⚠️ The countdown must not fire underneath a sweep that has already begun.

    Otherwise a gesture arriving just before the window closes gets its channels cut mid-move —
    intermittently, depending on timing, which is the worst possible way for this to be wrong.
    ``perform`` cancels the timer *before* arming anything else for exactly that reason."""
    await rig.service.perform(Gesture.CENTER, correlation_id=uuid4())
    await _settle(rig.service)
    await rig.clock.advance(_IDLE_RELAX_MS / 1000 / 2)

    await rig.service.perform(Gesture.NOD, correlation_id=uuid4())
    await rig.servo.moves.wait_for_steps(1)
    await rig.clock.advance(
        _IDLE_RELAX_MS / 1000
    )  # the OLD timer's moment, had it survived
    await asyncio.sleep(0)

    assert any(rig.servo.is_energised(axis.channel) for axis in rig.servo.axes), (
        "the previous gesture's relax timer fired underneath a live sweep"
    )
    await _settle(rig.service)


async def test_sleeping_relaxes_immediately_rather_than_waiting_out_the_timer(
    rig: Rig,
) -> None:
    """A robot that has just fallen asleep is a robot nobody is looking at.

    Waiting out ``idle_relax_ms`` there means humming into an empty room for the one interval
    where it is most obviously wrong — and #207 grades this with an ear, in a quiet room, which
    is the only instrument that can hear the difference."""
    await rig.service.perform(Gesture.CENTER, correlation_id=uuid4())
    await _settle(rig.service)
    assert any(rig.servo.is_energised(axis.channel) for axis in rig.servo.axes)

    await rig.bus.publish(_transitioned(to=RobotState.SLEEPING))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert not any(rig.servo.is_energised(axis.channel) for axis in rig.servo.axes)


async def test_sleeping_cancels_a_gesture_in_flight(rig: Rig) -> None:
    """A nod that finishes after the robot is asleep is a contradiction someone eventually
    watches happen — and it re-energises the channels the sleep just let go of."""
    await rig.service.perform(Gesture.NOD, correlation_id=uuid4())
    await rig.servo.moves.wait_for_steps(1)

    await rig.bus.publish(_transitioned(to=RobotState.SLEEPING))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await _settle(rig.service)
    await asyncio.sleep(0)

    assert not any(rig.servo.is_energised(axis.channel) for axis in rig.servo.axes)
    preempted = rig.collector.of(MotionGesturePreempted)
    assert [(e.gesture, e.by) for e in preempted] == [("nod", None)]  # type: ignore[attr-defined]


async def test_waking_up_does_not_move_anything(rig: Rig) -> None:
    """Only ``SLEEPING`` is this service's business on the state feed.

    Every other transition belongs to the face, and a service that gestured on each would be
    moving on ``IDLE → LISTENING`` — which fires whenever anyone speaks."""
    await rig.bus.publish(_transitioned(to=RobotState.IDLE))
    await rig.bus.publish(_transitioned(to=RobotState.LISTENING))
    await asyncio.sleep(0)

    assert rig.servo.moves == []
    assert rig.collector.events == []


# --- the GestureTools surface (#204) -----------------------------------------


async def test_look_at_declines_a_direction_this_rig_cannot_express(
    pan_only: Rig,
) -> None:
    """AC-8: an honest ``{ok: false}``, never a silent no-op **and never a lie**.

    Three possible answers and only one is acceptable. ``ACCEPTED`` would leave the model
    describing motion that never happened. A tool *error* would have it apologise for a
    malfunction. ``NO_AXIS`` is the robot working correctly and able to say what it cannot do."""
    outcome = await pan_only.service.look_at(Direction.UP, correlation_id=uuid4())

    assert outcome is LookAtResult.NO_AXIS
    assert pan_only.servo.moves == []
    assert pan_only.collector.of(MotionGestureStarted) == []


async def test_look_at_shares_the_preemption_path_with_affect_driven_gestures(
    rig: Rig,
) -> None:
    """One scheduler, not two.

    A voice-driven look and an affect-driven nod are the same kind of thing to the rig, so they
    contend through the same ``perform`` — which is why a look preempts a nod in flight and says
    so. Two separate paths would let the model's request and the robot's feelings drive the
    servos simultaneously, which on a shared rail is the failure #206 measures."""
    await rig.service.perform(Gesture.NOD, correlation_id=uuid4())
    await rig.servo.moves.wait_for_steps(1)

    await rig.service.look_at(Direction.LEFT, correlation_id=uuid4())
    await _settle(rig.service)

    preempted = rig.collector.of(MotionGesturePreempted)
    assert [(e.gesture, e.by) for e in preempted] == [("nod", "turn_left")]  # type: ignore[attr-defined]


async def test_an_affect_gesture_does_not_consume_the_look_at_cooldown(
    rig: Rig,
) -> None:
    """⚠️ The rate limit is on the **model**, not on the robot.

    The cooldown exists because a model that finds gesturing delightful would drive the servos
    continuously (AC-6). The robot's own affect-driven gestures are already rate-limited by how
    often affect changes, and charging them against the same budget would mean a lively
    conversation silently disables the user's ability to say "look left"."""
    await rig.service.perform(Gesture.NOD, correlation_id=uuid4())
    await _settle(rig.service)

    outcome = await rig.service.look_at(Direction.LEFT, correlation_id=uuid4())
    await _settle(rig.service)

    assert outcome is LookAtResult.ACCEPTED
