"""The proactive scheduler loop — one task, one heap, one wake event (SDS §10.3, #236b).

§10.3 is prescriptive about the shape, and about what it is *not*: *"One asyncio task. A min-heap of
``(next_fire_at, trigger_id)``. It sleeps until the earliest deadline. **Not cron. Not a task per
trigger. Not polling every second.**"*

Two properties do the work.

**The wake event.** §10.3: *"when §3.7.3's ``memory.fact_stored`` arrives and ``BehaviorService``
registers a new trigger, the sleeping task must recompute rather than snoozing until the old
deadline."* Without it, telling the robot about a 08:00 routine at 07:00 books it for tomorrow — the
loop is already asleep on a later deadline and never reconsiders. The bug is invisible on the second
morning, which is the worst place for it to become visible.

**The injected `Clock`.** The loop never touches wall time; it sleeps on the port. With a
:class:`~avid.adapters.clock.FakeClock` that makes three fire cycles a millisecond test instead of a
three-day wait, which is the whole reason `Clock` is a port at all (§9.3): *"waiting until 07:55 to
test the 07:55 code path is not a testing strategy."*

**Placement.** This is a *collaborator*, not a peer service — the same carve-out
``services/cue_bank.py`` already has in ``.importlinter``: a thing the port-owning service calls,
which imports only ``core``/``domain`` and so opens no path between services (P5). It is not on the
``service-independence`` list for that reason, and ``BehaviorService`` importing it is intended.

It publishes nothing and knows nothing about the bus. A due trigger is handed to an injected
callback; deciding what a due trigger *means* is §10.4's job, and keeping that out of here is what
makes the loop's own behaviour testable without a policy in the way.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import logging
from collections.abc import Awaitable, Callable

from avid.core.ports import Clock
from avid.core.tasks import spawn

_log = logging.getLogger("avid.services.scheduler")

#: How long to sleep when nothing is scheduled (SDS §10.3's skeleton).
#:
#: An hour, not forever, and not a second. The wake event already covers every registration, so
#: this bound is pure belt-and-braces against a missed wake — an idle robot pays one no-op
#: wakeup an hour, and a bug that would otherwise strand the scheduler until restart costs at
#: most an hour of proactivity instead of all of it.
IDLE_SLEEP_S = 3600.0

#: Nanoseconds per second — for comparing a monotonic span against a wall-clock one.
_NS_PER_S = 1_000_000_000

#: How far wall time and monotonic time may disagree across one sleep before we call it a
#: step rather than rounding (#345).
#:
#: Both readings are taken a few statements apart around an ``await``, so a second of slop
#: is ordinary. An NTP correction on a Pi with no RTC is measured in *hours*, so this
#: threshold does not need to be tight to separate the two — only loud enough to be
#: believed when it fires.
CLOCK_STEP_TOLERANCE_S = 2.0


class SchedulerLoop:
    """A min-heap of ``(fire_at, trigger_id)`` and one task that sleeps until the earliest.

    Registration is idempotent per trigger: :meth:`schedule` called twice for one id moves it
    rather than queueing it twice. Removal is **lazy** — the entry stays in the heap and is
    discarded when it surfaces — because a cancelled trigger is rare and a linear scan of the heap
    on every ``memory.fact_deleted`` is not. :attr:`_scheduled` is the authority on what is really
    due; the heap is only an ordering hint.

    ``max_sleep_s`` bounds **every** sleep, not just the idle one (#345). A deadline is a
    *wall-clock* instant and the sleep that waits for it is *monotonic*, so a wall-clock step
    invalidates an outstanding sleep without shortening it: on the rig the Pi booted with a
    33-hour-stale clock, computed a 9h38m delay for a booking that was really 35 minutes away,
    and slept straight through it. Worse than late — while parked, the trigger is never re-armed
    either, so the loop is *wedged* rather than merely behind. Re-deriving the delay at most every
    ``max_sleep_s`` puts a ceiling on both. The caller chooses the ceiling; this class has no
    opinion about what a tolerable lateness is (see ``BehaviorService``, which pins it to #339's
    grace window).
    """

    def __init__(
        self,
        *,
        clock: Clock,
        on_due: Callable[[int, int], Awaitable[None]],
        max_sleep_s: float = IDLE_SLEEP_S,
    ) -> None:
        self._clock = clock
        self._on_due = on_due
        self._max_sleep_s = max_sleep_s
        self._heap: list[tuple[int, int]] = []  # (fire_at, trigger_id)
        self._scheduled: dict[int, int] = {}  # trigger_id -> its current fire_at
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    # -- registration (called from the event loop, never blocking) ------------------

    def schedule(self, trigger_id: int, *, fire_at: int) -> None:
        """Register or move ``trigger_id`` to fire at ``fire_at`` (epoch seconds) and wake the loop.

        Waking on **every** registration rather than only on an earlier one is deliberate. The
        cheaper version — wake only when the new deadline beats the current head — is correct until
        the head is the trigger being *moved later*, at which point the loop sleeps on a deadline
        that no longer exists and fires something that is no longer due. One redundant wakeup is not
        worth the class of bug that saves.
        """
        self._scheduled[trigger_id] = fire_at
        heapq.heappush(self._heap, (fire_at, trigger_id))
        self._wake.set()

    def cancel(self, trigger_id: int) -> None:
        """Stop considering ``trigger_id``. Idempotent; an unknown id is not an error.

        Lazy: the heap entry survives and is discarded when it surfaces. What matters is that
        :attr:`_scheduled` no longer claims it, and that is checked at fire time.
        """
        self._scheduled.pop(trigger_id, None)
        self._wake.set()

    @property
    def pending(self) -> int:
        """How many triggers are actually scheduled — heap entries that are stale do not count."""
        return len(self._scheduled)

    def next_deadline(self) -> int | None:
        """The earliest live deadline, or ``None`` when nothing is scheduled. Diagnostic only."""
        return min(self._scheduled.values(), default=None)

    # -- lifecycle -----------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the loop as an owned task (``core/tasks.py::spawn``, never a bare create_task)."""
        if self._task is None:
            self._stopped = False
            self._task = spawn(self._run(), name="BehaviorService.scheduler")

    async def stop(self) -> None:
        """Cancel the loop and await its exit. Idempotent, and safe before :meth:`start`."""
        self._stopped = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # -- the loop ------------------------------------------------------------------

    async def _run(self) -> None:
        while not self._stopped:
            delay = self._delay_until_next()
            wall_before = self._clock.now()
            mono_before = self._clock.monotonic_ns()
            await self._sleep_or_wake(delay)
            self._report_clock_step(wall_before, mono_before)
            self._wake.clear()
            await self._fire_due()

    def _delay_until_next(self) -> float:
        """Seconds until the earliest live deadline, floored at zero and capped at the bound.

        The cap is the whole of #345's fix. Without it a deadline far in the future becomes one
        very long monotonic sleep, and a wall-clock correction during that sleep cannot shorten
        it — the loop wakes on the schedule the *wrong* clock dictated.

        The idle branch keeps :data:`IDLE_SLEEP_S` deliberately rather than the (possibly much
        smaller) cap: with nothing booked there is nothing a clock step could invalidate, and
        :meth:`schedule` wakes the loop the instant that stops being true.
        """
        deadline = self.next_deadline()
        if deadline is None:
            return IDLE_SLEEP_S
        return min(max(0.0, float(deadline - self._clock.now())), self._max_sleep_s)

    def _report_clock_step(self, wall_before: int, mono_before: int) -> None:
        """Say so, loudly, when wall time moved further than monotonic time across a sleep.

        Purely observational — the cap in :meth:`_delay_until_next` is what makes the loop
        *correct*; this is what makes the event *diagnosable*. §10.6's argument applied to the
        scheduler itself: from a silent log, a robot that slept through a morning and a scheduler
        that stopped working are the same picture. Tonight's nine-hour nap left no trace at all.
        """
        wall_s = float(self._clock.now() - wall_before)
        mono_s = (self._clock.monotonic_ns() - mono_before) / _NS_PER_S
        step_s = wall_s - mono_s
        if abs(step_s) <= CLOCK_STEP_TOLERANCE_S:
            return
        _log.warning(
            "wall clock stepped %+.1fs during a %.1fs sleep (wall moved %.1fs, monotonic "
            "%.1fs) — deadlines are re-derived from the corrected clock; a booking may now "
            "be due, or no longer due",
            step_s,
            mono_s,
            wall_s,
            mono_s,
        )

    async def _sleep_or_wake(self, delay: float) -> None:
        """Sleep ``delay`` on the injected clock, cut short by a registration.

        Deliberately **not** ``asyncio.wait_for(self._wake.wait(), timeout=delay)``, which is what
        §10.3's skeleton shows: that timeout is measured by the event loop's own clock, not the
        injected one, so under a ``FakeClock`` the test would sit through a real 3600-second sleep
        while virtual time stood still. Racing two awaitables is what keeps the ``Clock`` port
        actually in charge of time.
        """
        if self._wake.is_set():
            return
        sleeper = asyncio.ensure_future(self._clock.sleep(delay))
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pending in (sleeper, waker):
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending

    async def _fire_due(self) -> None:
        """Hand every trigger whose deadline has passed to the callback, earliest first.

        A stale heap entry — one whose trigger was cancelled or moved — is discarded here rather
        than searched for at cancel time. The dictionary is the authority; the heap is an ordering
        hint that is allowed to be wrong.

        A raising callback is logged and swallowed, and the loop continues. The alternative is one
        bad trigger taking the scheduler down and silently ending proactivity for the session —
        the same argument §3.5 makes for the bus swallowing subscriber failures, for the same
        reason: this loop is the only thing that will ever fire any of the others.
        """
        now = self._clock.now()
        while self._heap and self._heap[0][0] <= now:
            fire_at, trigger_id = heapq.heappop(self._heap)
            if self._scheduled.get(trigger_id) != fire_at:
                continue  # cancelled, or moved — this entry is a ghost
            del self._scheduled[trigger_id]
            try:
                # ``fire_at`` travels with the id: the callback needs to know which
                # *moment* came due, not merely which trigger. The store's
                # ``next_fire_at`` is not a substitute — the heap and the column are
                # allowed to diverge (a re-arm may schedule without persisting), so
                # grading the booking against the column reads the wrong clock (#339).
                await self._on_due(trigger_id, fire_at)
            except Exception:  # noqa: BLE001 - one bad trigger must not end proactivity
                _log.exception(
                    "scheduler callback failed for trigger %s; loop continues",
                    trigger_id,
                )


__all__ = ["CLOCK_STEP_TOLERANCE_S", "IDLE_SLEEP_S", "SchedulerLoop"]
