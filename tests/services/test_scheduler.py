"""The proactive scheduler loop (#236b, SDS §10.3).

Every test here runs in milliseconds on a ``FakeClock``, which is the point §10.3 makes about why
``Clock`` is a port: *"waiting until 07:55 to test the 07:55 code path is not a testing strategy."*
Three fire cycles, a day apart in virtual time, cost nothing.

The load-bearing test is ``test_a_registration_interrupts_a_sleep_already_in_progress``. §10.3 says
the wake event *"is what makes it correct"*, and the bug it prevents is nasty: without it, telling
the robot about an 08:00 routine at 07:00 books it for **tomorrow**, because the loop is already
asleep on a later deadline. Nothing errors. The robot simply says nothing on the first morning, and
works perfectly on the second.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

from avid.adapters.clock import FakeClock
from avid.services.scheduler import IDLE_SLEEP_S, SchedulerLoop


class _Recorder:
    """Collects the trigger ids handed to the callback, in order."""

    def __init__(self) -> None:
        self.fired: list[int] = []
        self.rang = asyncio.Event()

    async def __call__(self, trigger_id: int) -> None:
        self.fired.append(trigger_id)
        self.rang.set()


@pytest.fixture
async def rig() -> AsyncIterator[tuple[SchedulerLoop, FakeClock, _Recorder]]:
    clock = FakeClock()
    recorder = _Recorder()
    loop = SchedulerLoop(clock=clock, on_due=recorder)
    await loop.start()
    await asyncio.sleep(0)  # let the loop reach its first sleep
    try:
        yield loop, clock, recorder
    finally:
        await loop.stop()


async def _settle() -> None:
    """Yield enough times for the loop to wake, fire and re-arm.

    A handful of no-op yields rather than a sleep: the loop does a bounded amount of work per
    wake, and a real sleep here would be exactly the wall-clock dependency the ``Clock`` port
    exists to remove.
    """
    for _ in range(6):
        await asyncio.sleep(0)


# ── Firing ───────────────────────────────────────────────────────────────────────────────────


async def test_a_due_trigger_reaches_the_callback(
    rig: tuple[SchedulerLoop, FakeClock, _Recorder],
) -> None:
    loop, clock, recorder = rig
    loop.schedule(7, fire_at=clock.now() + 60)
    await _settle()
    assert recorder.fired == []  # not yet — time has not moved

    await clock.advance(60)
    await _settle()
    assert recorder.fired == [7]


async def test_three_cycles_a_day_apart_cost_no_wall_clock_time(
    rig: tuple[SchedulerLoop, FakeClock, _Recorder],
) -> None:
    """§10.3's own claim, exercised: the coffee scenario over three mornings, in microseconds."""
    loop, clock, recorder = rig
    for day in range(3):
        loop.schedule(7, fire_at=clock.now() + 86_400)
        await _settle()
        await clock.advance(86_400)
        await _settle()
        assert recorder.fired == [7] * (day + 1)


async def test_due_triggers_fire_earliest_first(
    rig: tuple[SchedulerLoop, FakeClock, _Recorder],
) -> None:
    """Several deadlines crossed by one advance still arrive in deadline order — the heap's job.
    Order matters because rule 5's global cooldown means the first one through takes the slot."""
    loop, clock, recorder = rig
    loop.schedule(3, fire_at=clock.now() + 300)
    loop.schedule(1, fire_at=clock.now() + 100)
    loop.schedule(2, fire_at=clock.now() + 200)
    await _settle()

    await clock.advance(400)
    await _settle()
    assert recorder.fired == [1, 2, 3]


async def test_a_fired_trigger_does_not_fire_again(
    rig: tuple[SchedulerLoop, FakeClock, _Recorder],
) -> None:
    """Firing consumes the registration. Re-arming is the caller's decision, because only it knows
    whether the rule has a next occurrence — a finite ``COUNT=`` that has run out must simply stop."""
    loop, clock, recorder = rig
    loop.schedule(7, fire_at=clock.now() + 60)
    await _settle()
    await clock.advance(60)
    await _settle()

    await clock.advance(86_400)
    await _settle()
    assert recorder.fired == [7]
    assert loop.pending == 0


# ── The wake event — §10.3's "what makes it correct" ─────────────────────────────────────────


async def test_a_registration_interrupts_a_sleep_already_in_progress(
    rig: tuple[SchedulerLoop, FakeClock, _Recorder],
) -> None:
    """The one §10.3 singles out.

    The loop is asleep on a deadline a week away. A routine registered for one minute from now must
    fire in one minute — not in a week. Without the wake event the loop never recomputes, and the
    failure is silent: the robot says nothing on the first morning and works perfectly on the
    second, which is a bug nobody reports and nobody finds.
    """
    loop, clock, recorder = rig
    loop.schedule(1, fire_at=clock.now() + 604_800)  # a week out
    await _settle()

    loop.schedule(2, fire_at=clock.now() + 60)  # ...and now this
    await _settle()

    await clock.advance(60)
    await _settle()
    assert recorder.fired == [2]


async def test_an_idle_loop_still_wakes_on_a_registration(
    rig: tuple[SchedulerLoop, FakeClock, _Recorder],
) -> None:
    """With an empty heap the loop is parked on the hour-long idle sleep. A first-ever trigger must
    not have to wait it out — which is the state a freshly-booted robot is in until the user
    mentions their first routine."""
    loop, clock, recorder = rig
    assert loop.next_deadline() is None

    loop.schedule(1, fire_at=clock.now() + 30)
    await _settle()
    await clock.advance(30)
    await _settle()
    assert recorder.fired == [1]


async def test_moving_a_trigger_later_does_not_fire_it_early(
    rig: tuple[SchedulerLoop, FakeClock, _Recorder],
) -> None:
    """The case that argues for waking on *every* registration rather than only on an earlier one.

    The cheap optimisation — wake only when the new deadline beats the head — leaves the loop
    asleep on a deadline that no longer exists when the head itself is moved later. This is
    §7.8's supersession in scheduler terms: "coffee at 08:00" corrected to 09:00 must not still
    arrive at 08:00.
    """
    loop, clock, recorder = rig
    loop.schedule(7, fire_at=clock.now() + 60)
    await _settle()
    loop.schedule(7, fire_at=clock.now() + 3600)  # the same trigger, an hour later
    await _settle()

    await clock.advance(120)
    await _settle()
    assert recorder.fired == []  # the old deadline is a ghost

    await clock.advance(3600)
    await _settle()
    assert recorder.fired == [7]


async def test_registering_twice_schedules_one_trigger_not_two(
    rig: tuple[SchedulerLoop, FakeClock, _Recorder],
) -> None:
    """A routine that is re-stated is still one routine. Two heap entries would mean the robot
    mentions coffee twice every morning, which looks like a working feature until someone counts."""
    loop, clock, recorder = rig
    loop.schedule(7, fire_at=clock.now() + 100)
    loop.schedule(7, fire_at=clock.now() + 200)
    assert loop.pending == 1

    await clock.advance(300)
    await _settle()
    assert recorder.fired == [7]


# ── Cancellation ─────────────────────────────────────────────────────────────────────────────


async def test_a_cancelled_trigger_never_fires(
    rig: tuple[SchedulerLoop, FakeClock, _Recorder],
) -> None:
    """``memory.fact_deleted`` — and UC-07's "forget that". A schedule the user withdrew consent
    for must not go off, which is a privacy property, not a convenience one."""
    loop, clock, recorder = rig
    loop.schedule(7, fire_at=clock.now() + 60)
    await _settle()
    loop.cancel(7)

    await clock.advance(120)
    await _settle()
    assert recorder.fired == []
    assert loop.pending == 0


async def test_cancelling_an_unknown_trigger_is_not_an_error(
    rig: tuple[SchedulerLoop, FakeClock, _Recorder],
) -> None:
    """``memory.fact_deleted`` arrives for every fact, most of which never had a schedule."""
    loop, _clock, _recorder = rig
    loop.cancel(999)
    loop.cancel(999)
    assert loop.pending == 0


async def test_a_stale_heap_entry_is_discarded_not_fired(
    rig: tuple[SchedulerLoop, FakeClock, _Recorder],
) -> None:
    """Removal is lazy — the heap entry survives a cancel and is dropped when it surfaces. This is
    the test that says the dictionary is the authority and the heap is only an ordering hint."""
    loop, clock, recorder = rig
    loop.schedule(1, fire_at=clock.now() + 60)
    loop.schedule(2, fire_at=clock.now() + 120)
    await _settle()
    loop.cancel(1)

    await clock.advance(200)
    await _settle()
    assert recorder.fired == [2]


# ── Robustness and lifecycle ─────────────────────────────────────────────────────────────────


async def test_a_raising_callback_does_not_end_proactivity() -> None:
    """One bad trigger must not take the scheduler down — the same argument §3.5 makes for the bus
    swallowing subscriber failures, and for a sharper reason: this loop is the only thing that will
    ever fire any of the others, so its death is silent and total."""
    clock = FakeClock()
    fired: list[int] = []

    async def _boom(trigger_id: int) -> None:
        fired.append(trigger_id)
        if trigger_id == 1:
            raise RuntimeError("this trigger is broken")

    loop = SchedulerLoop(clock=clock, on_due=_boom)
    await loop.start()
    await asyncio.sleep(0)
    try:
        loop.schedule(1, fire_at=clock.now() + 60)
        loop.schedule(2, fire_at=clock.now() + 120)
        await _settle()
        await clock.advance(200)
        await _settle()
        assert fired == [1, 2], "the second trigger fired despite the first raising"
    finally:
        await loop.stop()


async def test_the_loop_is_an_owned_task_by_name() -> None:
    """``core/tasks.py::spawn``, never a bare ``create_task`` — so a death is logged rather than
    swallowed by a dropped task reference, and the name shows up in a stack dump."""
    clock = FakeClock()
    loop = SchedulerLoop(clock=clock, on_due=_Recorder())
    await loop.start()
    try:
        names = {task.get_name() for task in asyncio.all_tasks()}
        assert "BehaviorService.scheduler" in names
    finally:
        await loop.stop()


async def test_stop_is_idempotent_and_safe_before_start() -> None:
    clock = FakeClock()
    loop = SchedulerLoop(clock=clock, on_due=_Recorder())
    await loop.stop()  # never started
    await loop.start()
    await loop.stop()
    await loop.stop()
    assert "BehaviorService.scheduler" not in {
        task.get_name() for task in asyncio.all_tasks()
    }


async def test_an_empty_heap_parks_on_the_idle_sleep() -> None:
    """An hour, not a busy-poll and not forever. §10.3: *"Not polling every second."* The wake
    event covers every registration, so the bound is belt-and-braces — but a scheduler that spun
    would show up as CPU on a Pi with a 1-core vision budget, and one that slept forever would
    strand proactivity until restart if a wake were ever missed."""
    clock = FakeClock()
    slept: list[float] = []
    original = clock.sleep

    async def _record(seconds: float) -> None:
        slept.append(seconds)
        await original(seconds)

    clock.sleep = _record  # type: ignore[method-assign]  # a probe on the fake, not the port
    loop = SchedulerLoop(clock=clock, on_due=_Recorder())
    await loop.start()
    try:
        await _settle()
        assert slept and slept[0] == IDLE_SLEEP_S
    finally:
        with contextlib.suppress(Exception):
            await loop.stop()
