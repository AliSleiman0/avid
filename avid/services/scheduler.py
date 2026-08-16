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


class SchedulerLoop:
    """A min-heap of ``(fire_at, trigger_id)`` and one task that sleeps until the earliest.

    Registration is idempotent per trigger: :meth:`schedule` called twice for one id moves it
    rather than queueing it twice. Removal is **lazy** — the entry stays in the heap and is
    discarded when it surfaces — because a cancelled trigger is rare and a linear scan of the heap
    on every ``memory.fact_deleted`` is not. :attr:`_scheduled` is the authority on what is really
    due; the heap is only an ordering hint.
    """

    def __init__(
        self, *, clock: Clock, on_due: Callable[[int], Awaitable[None]]
    ) -> None:
        self._clock = clock
        self._on_due = on_due
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
            await self._sleep_or_wake(delay)
            self._wake.clear()
            await self._fire_due()

    def _delay_until_next(self) -> float:
        """Seconds until the earliest live deadline, floored at zero, or :data:`IDLE_SLEEP_S`."""
        deadline = self.next_deadline()
        if deadline is None:
            return IDLE_SLEEP_S
        return max(0.0, float(deadline - self._clock.now()))

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
                await self._on_due(trigger_id)
            except Exception:  # noqa: BLE001 - one bad trigger must not end proactivity
                _log.exception(
                    "scheduler callback failed for trigger %s; loop continues",
                    trigger_id,
                )


__all__ = ["IDLE_SLEEP_S", "SchedulerLoop"]
