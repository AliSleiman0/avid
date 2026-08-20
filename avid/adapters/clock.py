"""Clock adapters — the real one and its fake (AVID-12, SDS §9.3).

Two implementations of the :class:`~avid.core.ports.Clock` port:

* :class:`SystemClock` — real time from the stdlib. The injectable, full-``Clock``
  adapter (with ``sleep``) that AVID-14's composition root wires into the bus and
  services. It supersedes the bus's private ``_TimeSource``/``_SystemClock`` stand-in
  (:mod:`avid.core.event_bus`), which stays only as the bus's minimal internal default.
* :class:`FakeClock` — virtual time the test drives by hand. It is *why* M10's gate,
  "the coffee scenario, unprompted", is a ~40 ms test instead of a wait until 07:55
  (SDS §9.3, §14.5): ``advance``/``advance_to`` move time forward and wake the coroutines
  sleeping on this clock, deterministically, in deadline order.

Both are constructed only by the composition root or a test fixture (P3). ``now()`` is
epoch **seconds** and ``monotonic_ns()`` is a monotonic nanosecond counter; in
``FakeClock`` a single advancing quantity drives both, so the *passage of time* can never
drift them apart — wall for humans, monotonic for arithmetic (SDS §9.1.1).

⚠️ A **wall-clock step** is the one thing that does separate them, and on the Pi it happens
every offline boot: no RTC, so the system comes up on a restored clock and NTP corrects it
later. :meth:`FakeClock.step_wall_clock` exists to state exactly that in a test, because
until #345 the fake could not express the defect at all — see its docstring.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

_NS_PER_S = 1_000_000_000

# A fixed, timezone-anchored default so a fresh FakeClock reads the same instant on every
# machine and every run — determinism is the whole point of a fake clock. Midnight UTC
# gives advance_to("07:55") a clean 7h55m first hop.
_DEFAULT_START = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())


class SystemClock:
    """Real time, straight from the stdlib — the production :class:`Clock` adapter."""

    def now(self) -> int:
        """Epoch **seconds**, wall clock — for logs and persistence (SDS §8.2)."""
        return int(time.time())

    def monotonic_ns(self) -> int:
        """``time.monotonic_ns()`` — for latency math (SDS §9.1.1)."""
        return time.monotonic_ns()

    async def sleep(self, seconds: float) -> None:
        """The real, awaitable delay — a thin pass-through to ``asyncio.sleep``."""
        await asyncio.sleep(seconds)


@dataclass(slots=True)
class _Sleeper:
    """One coroutine parked in :meth:`FakeClock.sleep`: the virtual ``monotonic_ns``
    deadline it is waiting for, and the future whose resolution resumes it."""

    deadline_ns: int
    future: asyncio.Future[None]


class FakeClock:
    """Manually-driven virtual time — the :class:`Clock` fake (SDS §9.3, P6).

    Time does not pass on its own: it moves only when a test calls :meth:`advance` or
    :meth:`advance_to`. Coroutines that :meth:`sleep` on this clock block until an advance
    crosses their deadline, at which point they wake in deadline order. ``now()`` and
    ``monotonic_ns()`` are both derived from one internal elapsed-nanosecond counter, so
    they advance together by construction.

    :meth:`step_wall_clock` is the deliberate exception, and the only way to make the two
    readings disagree.
    """

    def __init__(
        self, *, start: int = _DEFAULT_START, tz: timezone = timezone.utc
    ) -> None:
        # One source of truth for the passage of time: ns elapsed since `start`.
        # now() and monotonic_ns() are both pure functions of it, so they cannot drift.
        self._elapsed_ns = 0
        self._epoch0 = start
        # `tz` is used only by advance_to, to map a wall time-of-day to an epoch instant
        # without depending on the host's local timezone (which would make CI flaky).
        self._tz = tz
        self._sleepers: list[_Sleeper] = []

    def now(self) -> int:
        """Epoch **seconds** (SDS §8.2), advanced only by :meth:`advance`."""
        return self._epoch0 + self._elapsed_ns // _NS_PER_S

    def monotonic_ns(self) -> int:
        """Monotonic nanoseconds (SDS §9.1.1) — same elapsed counter as :meth:`now`."""
        return self._elapsed_ns

    @property
    def sleepers(self) -> int:
        """How many coroutines are currently parked on this clock.

        Exists for one recurring test hazard: :meth:`advance` wakes only the sleepers it
        **crosses**, so a task that has not yet reached its ``sleep()`` registers a deadline
        *after* the advance and then waits for a crossing that never comes. ``spawn()`` returns
        before its coroutine has run, which makes that a genuine race rather than a theoretical
        one — and one that surfaces as "passes on two CI legs, fails on the slower third".

        A test that means "let the loop settle, then move time" can now say so
        (``while not clock.sleepers: await asyncio.sleep(0)``) instead of guessing at a number of
        yields.
        """
        return len(self._sleepers)

    async def sleep(self, seconds: float) -> None:
        """Park until the clock is advanced ``seconds`` past *now*.

        A non-positive delay yields to the loop once and returns. Otherwise the caller
        blocks on a future keyed to a virtual deadline; :meth:`advance` resolves it. The
        ``finally`` deregisters the sleeper so a cancelled sleep leaves nothing behind.
        """
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        deadline_ns = self._elapsed_ns + round(seconds * _NS_PER_S)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        sleeper = _Sleeper(deadline_ns=deadline_ns, future=future)
        self._sleepers.append(sleeper)
        try:
            await future
        finally:
            if sleeper in self._sleepers:
                self._sleepers.remove(sleeper)

    async def advance(self, seconds: float) -> None:
        """Move virtual time forward by ``seconds``, waking every sleeper it crosses.

        Forward only — a clock never runs backward, so a negative delta is a
        :class:`ValueError`. Sleepers whose deadline is reached are woken in **deadline
        order**; the trailing ``asyncio.sleep(0)`` lets those coroutines make progress
        before this call returns.
        """
        if seconds < 0:
            raise ValueError(f"advance() moves time forward only; got {seconds}")
        target = self._elapsed_ns + round(seconds * _NS_PER_S)
        self._wake_through(target)
        self._elapsed_ns = target
        await asyncio.sleep(0)

    async def advance_to(self, wall: str) -> None:
        """Advance to the next instant whose wall clock reads ``wall`` (``"HH:MM"`` or
        ``"HH:MM:SS"``), in this clock's timezone.

        Strictly forward: if that time-of-day is already at or behind *now*, it rolls to
        the next day — so ``advance_to`` can never move time backward.
        """
        # noqa: DTZ007 — parses a time-of-day only; sole use is the hour/minute/
        # second fields, applied below to the tz-aware `current`. The naive value
        # never stands for an instant, so attaching a tzinfo here would mislead.
        target_time = datetime.strptime(  # noqa: DTZ007
            wall, "%H:%M:%S" if wall.count(":") == 2 else "%H:%M"
        )
        current = datetime.fromtimestamp(self.now(), tz=self._tz)
        target = current.replace(
            hour=target_time.hour,
            minute=target_time.minute,
            second=target_time.second,
            microsecond=0,
        )
        if target <= current:
            target += timedelta(days=1)
        await self.advance(target.timestamp() - self.now())

    def step_wall_clock(self, seconds: float) -> None:
        """Move the **wall** clock without moving the monotonic one — an NTP step (#345).

        Deliberately not :meth:`advance`. ``_elapsed_ns`` does not move, so
        :meth:`monotonic_ns` is unchanged and **no parked sleeper wakes**: a coroutine that
        asked for 9 hours still has 9 hours of monotonic time to wait, no matter what the
        wall clock now says. That is precisely what the real system does, and precisely
        what this class otherwise makes impossible to state — ``now()`` and
        ``monotonic_ns()`` share one counter so that the passage of time cannot drift them
        apart, which also meant the *only* way they drift in production had no expression
        in the fake. #345 shipped because of that gap.

        On the Pi it is not an exotic case. There is no RTC, so an offline boot restores a
        stale clock and NTP steps it forward once the network arrives — on 2026-08-19 by
        33 hours, while the scheduler slept through the booking that step revealed.

        Negative is legal and is **not** the mistake :meth:`advance` guards against: wall
        clocks genuinely do step backward (an NTP correction the other way), and it is only
        *monotonic* time that may never retreat.
        """
        self._epoch0 += round(seconds)

    def _wake_through(self, target_ns: int) -> None:
        """Resolve, in deadline order, every sleeper due at or before ``target_ns``.

        Resolving the futures in deadline order schedules their resumptions in that order,
        which is the sleepers-wake-in-the-right-order guarantee (AC). Draining is a `while`
        rather than one pass so a sleeper that itself sleeps again is handled next advance.
        """
        due = sorted(
            (s for s in self._sleepers if s.deadline_ns <= target_ns),
            key=lambda s: s.deadline_ns,
        )
        for sleeper in due:
            self._sleepers.remove(sleeper)
            if not sleeper.future.done():
                sleeper.future.set_result(None)
