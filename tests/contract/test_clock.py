"""Contract suite for the ``Clock`` port (AVID-12, SDS §9.3).

A port's contract test runs against *every* adapter, real and fake, so the fake can
never quietly drift from the real thing (P6, SDS §3.9.2). The shared tier below is
parametrized over both :class:`SystemClock` and :class:`FakeClock` and asserts the
invariants that must hold for either — this is AC "passes against both". The
``FakeClock``-specific tier then pins the virtual-time behaviour the real clock cannot
have: exact ``advance``, ``advance_to``, and deterministic sleeper wake ordering.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import pytest

from avid.adapters.clock import FakeClock, SystemClock
from avid.core.ports import Clock

_NS_PER_S = 1_000_000_000


# --- shared contract: every Clock adapter must satisfy it -------------------

# Each case is a clock plus a coroutine that advances it by a small span — the fake by
# fiat, the real one by an actually-tiny sleep — so one test body exercises both.
ClockCase = tuple[Clock, Callable[[], Awaitable[None]]]


@pytest.fixture(params=["SystemClock", "FakeClock"])
def clock_case(request: pytest.FixtureRequest) -> ClockCase:
    if request.param == "SystemClock":
        system = SystemClock()

        async def advance_system() -> None:
            await system.sleep(0.02)

        return system, advance_system

    fake = FakeClock()

    async def advance_fake() -> None:
        await fake.advance(2.0)

    return fake, advance_fake


def test_adapter_satisfies_the_clock_port(clock_case: ClockCase) -> None:
    clock, _ = clock_case
    assert isinstance(clock, Clock)


def test_readings_are_ints(clock_case: ClockCase) -> None:
    clock, _ = clock_case
    assert isinstance(clock.now(), int)
    assert isinstance(clock.monotonic_ns(), int)


def test_monotonic_never_goes_backward(clock_case: ClockCase) -> None:
    clock, _ = clock_case
    assert clock.monotonic_ns() <= clock.monotonic_ns()


async def test_sleep_zero_completes(clock_case: ClockCase) -> None:
    clock, _ = clock_case
    await clock.sleep(0)  # returns promptly, never hangs


async def test_advancing_moves_both_readings_forward(clock_case: ClockCase) -> None:
    """AC "move together": after time passes, monotonic advances and wall never rewinds
    — the same direction on one shared timeline. (The fake pins the exact deltas below.)"""
    clock, advance = clock_case
    now0, mono0 = clock.now(), clock.monotonic_ns()
    await advance()
    assert clock.monotonic_ns() > mono0
    assert clock.now() >= now0


# --- FakeClock-specific: the virtual-time contract -------------------------


async def test_fake_advance_moves_now_and_monotonic_by_the_same_span() -> None:
    clock = FakeClock()
    now0, mono0 = clock.now(), clock.monotonic_ns()
    await clock.advance(60)
    assert clock.now() - now0 == 60
    assert clock.monotonic_ns() - mono0 == 60 * _NS_PER_S


async def test_fake_advance_rejects_going_backward() -> None:
    clock = FakeClock()
    with pytest.raises(ValueError, match="forward only"):
        await clock.advance(-1)


async def test_fake_advance_to_reaches_the_wall_time() -> None:
    clock = FakeClock()  # default start: midnight UTC
    await clock.advance_to("07:55")
    reached = datetime.fromtimestamp(clock.now(), tz=timezone.utc)
    assert (reached.hour, reached.minute) == (7, 55)


async def test_fake_advance_to_rolls_over_when_the_time_already_passed() -> None:
    clock = FakeClock()
    await clock.advance_to("07:55")
    before = clock.now()
    await clock.advance_to("07:55")  # same time-of-day, now behind us → next day
    assert clock.now() - before == 86_400


async def test_fake_sleep_blocks_until_the_clock_is_advanced() -> None:
    clock = FakeClock()
    woke: list[int] = []

    async def sleeper() -> None:
        await clock.sleep(30)
        woke.append(clock.now())

    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0)  # let the sleeper register its deadline
    assert woke == []  # time has not moved, so it is still parked

    await clock.advance(30)
    assert woke == [clock.now()]  # advancing past its deadline woke it
    await task


async def test_fake_cancelled_sleep_deregisters_itself() -> None:
    """A cancelled sleep must leave nothing parked, or a later advance would try to wake
    a coroutine that is already gone."""
    clock = FakeClock()
    task = asyncio.create_task(clock.sleep(30))
    await asyncio.sleep(0)  # let it register
    assert clock._sleepers  # parked
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clock._sleepers == []  # the finally cleaned it up


async def test_fake_sleepers_wake_in_deadline_order() -> None:
    clock = FakeClock()
    order: list[str] = []

    async def sleeper(label: str, seconds: float) -> None:
        await clock.sleep(seconds)
        order.append(label)

    # Register the later deadline first, to prove ordering is by deadline, not arrival.
    long_task = asyncio.create_task(sleeper("long", 10))
    short_task = asyncio.create_task(sleeper("short", 5))
    await asyncio.sleep(0)  # let both register

    await clock.advance(10)  # crosses both deadlines in one advance
    assert order == ["short", "long"]
    await asyncio.gather(long_task, short_task)
