"""The stepper — bounded, net-zero desk steps, one at a time, and a stop that beats a fall (#400).

The wheels' sibling of ``MotionService`` (#203), shaped the same way for the same reasons: it
owns the :class:`~avid.core.ports.Drive` and :class:`~avid.core.ports.EdgeSensor` ports and is
the only thing in the system that calls ``run``/``stop``/``clear`` (SDS §9.1.4, a test asserts
it); it does not decide what a step *is* — that is :func:`~avid.domain.drive.plan_step` (pure);
and what is left is scheduling, safety, and honesty about where the robot is.

**What this service adds over the servo one is the floor.** A nod that goes wrong leaves a head
at an odd angle; a step that goes wrong leaves a robot on the carpet. So three things
``MotionService`` never had to do are the whole of this module (SDS §3.9.5, §12.1 F-13/F-14):

* **It asks before it moves, and keeps asking while it moves.** ``EdgeSensor.clear()`` is read
  before every moving leg and polled every ``[drive.edge] poll_ms`` during one. An edge stops
  the motors through the port — a direct awaited call, never an event — and if the leg was
  heading *toward* the edge the service **retreats immediately**, by the distance it just drove,
  because moving away from an edge is the safe direction. Then it **latches**: no step is
  accepted until ``clear()`` has held for ``clear_hold_ms``, so a hand that waved once does not
  become a hand that must wave twice.
* **It keeps an odometer of its own**, from the milliseconds :meth:`~avid.core.ports.Drive.run`
  reports, not from the plan. A leg cut short by an edge, a preemption or a fault leaves an
  *offset*; the next step **homes first** (:func:`~avid.domain.drive.homing_leg`), so an abort is
  a delay and not a drift. The adapter's fake keeps a second odometer, and the two agreeing is
  what a service test asserts.
* **It refuses to run past the budget**, before any leg, from the odometer. Every plan is
  already bounded by the planner, so this guard should never fire — which is exactly why it is
  a loud ``step_aborted(reason="budget")`` row rather than a silent clamp.

**Steps run only in ``RobotState.IDLE``** (SDS §3.9.5). N20 gearboxes are audible: a motor
running during LISTENING feeds itself to the microphone, and one running during SPEAKING is a
robot that walks off mid-sentence. Any transition out of IDLE **preempts** a step in flight,
and SLEEPING additionally cuts the motors — a sleeping robot that rolls is a robot that did not
go to sleep. The idle-step loop is the only trigger in v1: no affect maps to a step (#202's
restraint, applied to a louder actuator), and #477 is the first candidate for a second.

**Two clocks, and which is which.** The *scheduling* — the idle band, the clear-hold — sleeps on
the injected :class:`~avid.core.ports.Clock`, so a test drives an hour of idle in one call. The
*physical* parts — the dwell between legs, the sensor poll during a leg — sleep on
``asyncio.sleep``, because they are tied to a motor that is genuinely turning, and the device
fake turns it on wall clock for the same reason ``FakeServo`` sweeps on wall clock: a physical
duration is not fakeable time. The same split ``MotionService`` makes between its relax timer
and its servo sweep.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections import Counter
from collections.abc import Sequence
from typing import cast
from uuid import UUID, uuid4

from avid.core.envelope import envelope
from avid.core.event_bus import (
    DEFAULT_MAXSIZE,
    Handler,
    OverflowPolicy,
    Subscription,
)
from avid.core.ports import Clock, Drive, EdgeSensor, EventBus
from avid.core.tasks import spawn
from avid.domain import (
    BUDGET,
    EDGE,
    FAULT,
    PREEMPTED,
    DriveStepAborted,
    DriveStepCompleted,
    DriveStepStarted,
    Gesture,
    Heading,
    Leg,
    RobotState,
    StateTransitioned,
    StepGeometry,
    homing_leg,
    net_mm,
    plan_step,
)

_log = logging.getLogger(__name__)

_SOURCE = "DriveService"

_NS_PER_MS = 1_000_000

# The odometer's tolerance for "at origin", in millimetres. Below this a homing leg would be a
# jolt shorter than a slice of the fake's clock; the planner floors a leg at 1 ms anyway.
_HOME_TOLERANCE_MM = 0.5


class _EdgeSeen(Exception):
    """Raised inside a leg when the sensors reported an edge; carries the leg it cut."""

    def __init__(self, leg: Leg) -> None:
        super().__init__(leg.heading)
        self.leg = leg


class DriveService:
    """Turns a step intent into bounded wheel motion, and stops for the desk edge (SDS §3.9.5).

    Shaped to SDS §9.2 (``name`` / ``start`` / ``stop`` / ``subscriptions``) and depends on the
    :class:`~avid.core.ports.Drive`, :class:`~avid.core.ports.EdgeSensor`,
    :class:`~avid.core.ports.EventBus` and :class:`~avid.core.ports.Clock` **Protocols**, never
    a concrete adapter (P2) — the identical service walks a fake odometer in CI and an L9110S on
    the desk.
    """

    name = _SOURCE

    def __init__(
        self,
        *,
        bus: EventBus,
        drive: Drive,
        edge: EdgeSensor,
        clock: Clock,
        geometry: StepGeometry,
        edge_poll_ms: int,
        edge_clear_hold_ms: int,
        idle_step_interval_min_s: float,
        idle_step_interval_max_s: float,
        rng: random.Random | None = None,
    ) -> None:
        self._bus = bus
        self._drive = drive
        self._edge = edge
        self._clock = clock
        self._geometry = geometry
        self._poll_s = edge_poll_ms / 1000
        self._clear_hold_s = edge_clear_hold_ms / 1000
        self._idle_min_s = idle_step_interval_min_s
        self._idle_max_s = idle_step_interval_max_s
        # Injectable only so a test can seed it, exactly as MotionService's drift is.
        self._rng = rng if rng is not None else random.Random()

        # The step in flight and what it is — held together for the same reason MotionService
        # holds them together: preemption needs the task to cancel and the name to report.
        self._task: asyncio.Task[None] | None = None
        self._current: Gesture | None = None
        # The idle-step scheduler. Long-lived, unlike the steps it arms.
        self._idle_task: asyncio.Task[None] | None = None
        # Which way the next idle step goes. Alternated, so an unattended robot shuffles rather
        # than always leaning the same way first.
        self._next_idle = Gesture.STEP_TOWARD

        # Whether the robot is IDLE — the only state a step may run in. ``False`` until the
        # state machine says otherwise: a service that assumed IDLE at construction would step
        # during BOOTING.
        self._idle = False
        # The service's own odometer: mm from origin along the forward axis, integrated from what
        # ``Drive.run`` reported. Signed; ``+`` is toward the user.
        self._offset_mm = 0.0
        # Set by an edge abort; cleared only once ``clear()`` has held for the configured time.
        self._edge_latched = False

        # Counters an operator reads, not facts anyone is notified of.
        self.steps_performed = 0
        self.steps_declined = 0
        self.steps_aborted: Counter[str] = Counter()

    # --- SDS §9.2 service shape ----------------------------------------------------------

    async def start(self) -> None:
        """Launch the idle-step scheduler. Idempotent.

        Its first step is a random interval away — minutes, by the shipped band — so booting it
        here costs nothing and means an unattended robot eventually shifts its weight without
        anyone having spoken to it. Steps are otherwise never spawned from here: the first thing
        anyone should see a wheel do is nothing.
        """
        if self._idle_task is None:
            self._idle_task = spawn(self._idle_steps(), name="DriveService.idle_steps")

    async def stop(self) -> None:
        """Cancel the scheduler and any step in flight, and **cut the motors**. Idempotent.

        ⚠️ The motor stop is unconditional and last. Everything else this service can get wrong
        stops when the process stops; a wheel left turning does not. It is the one failure mode
        that outlives the program, and it is why the composition root stops this service first.
        """
        idle, self._idle_task = self._idle_task, None
        if idle is not None and not idle.done():
            idle.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await idle
        task, self._task = self._task, None
        self._current = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._stop_motors()

    def subscriptions(self) -> Sequence[Subscription]:
        """Declare, do not register (SDS §9.2).

        One subscription: the state feed, read **independently** of every other reader of it.
        Whether the robot is IDLE is the whole of this service's policy input, and it needs no
        other service to tell it (P5). DROP_OLDEST: only the newest state is worth acting on.
        """
        return (
            Subscription(
                event_type=StateTransitioned,
                handler=cast(Handler, self._on_state_transitioned),
                name="DriveService.state_transitioned",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
        )

    # --- the state feed ------------------------------------------------------------------

    async def _on_state_transitioned(self, event: StateTransitioned) -> None:
        """Leaving IDLE preempts; SLEEPING also cuts the motors, immediately.

        No staleness guard, on purpose (the same reasoning as ``MotionService``'s): stopping is
        idempotent and harmless, so a late transition costs nothing, while skipping one would
        leave a wheel turning through a conversation.
        """
        self._idle = event.to is RobotState.IDLE
        if self._idle:
            return
        await self._preempt(correlation_id=event.correlation_id)
        if event.to is RobotState.SLEEPING:
            await self._stop_motors()

    # --- the step path -------------------------------------------------------------------

    async def perform(self, gesture: Gesture, *, correlation_id: UUID) -> None:
        """Start *gesture*, preempting whatever is running. Returns once it is **armed**.

        Like ``MotionService.perform``, it does not await the movement — a step is a second of
        physical motion, and the caller is a handler or a scheduler that must not sit for it.
        It *is* a coroutine because the refusals below read the sensors, and because the
        preemption publish must be queued before the new step's ``step_started``.

        The four ways a well-formed request produces no step, each logged with its reason:

        * an **empty plan** — this rig has no wheels, or the gesture is not a step (SDS §3.9.3);
        * **not IDLE** — steps run in IDLE and nowhere else (SDS §3.9.5);
        * an **edge latch** that has not cleared — ``clear()`` must hold for the configured time
          after an edge abort before the wheels are trusted again;
        * an **edge right now**, read before the first leg.
        """
        legs = plan_step(gesture, self._drive.capabilities, self._geometry)
        if not legs:
            _log.debug(
                "no plan for %s on this rig — no-op [correlation_id=%s]",
                gesture.name.lower(),
                correlation_id,
            )
            return
        if not self._idle:
            self.steps_declined += 1
            _log.debug(
                "declined %s: robot is not IDLE [correlation_id=%s]",
                gesture.name.lower(),
                correlation_id,
            )
            return
        if self._edge_latched and not await self._edge_has_cleared():
            self.steps_declined += 1
            _log.info(
                "declined %s: edge latch has not cleared [correlation_id=%s]",
                gesture.name.lower(),
                correlation_id,
            )
            return
        await self._preempt(correlation_id=correlation_id)
        self._current = gesture
        self._task = spawn(
            self._run(gesture, legs, correlation_id=correlation_id),
            name=f"DriveService.{gesture.name.lower()}",
        )

    async def _preempt(self, *, correlation_id: UUID) -> None:
        """Cancel the step in flight, if any, and report that it was cut short.

        Synchronous cancel, as ``MotionService._preempt``; the leg runner's cancellation
        handler is what stops the motors, and the publish is what makes the cut a fact.
        """
        task, self._task = self._task, None
        cancelled, self._current = self._current, None
        if task is None or task.done():
            return
        task.cancel()
        # Await the cut step so the motors are stopped and the odometer settled BEFORE the
        # abort is published — but never swallow a cancellation aimed at *this* task. This
        # runs inside a bus handler, and a blanket ``suppress(CancelledError)`` here ate the
        # worker's own cancellation on ``bus.stop()``: the worker then looped back to its
        # queue marked "cancelling" and the bus never stopped. ``Task.cancelling()`` is the
        # 3.11+ idiom that tells the two apart.
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        if (
            cancelled is None
        ):  # pragma: no cover - a task always has a gesture; defensive
            return
        await self._publish_aborted(cancelled, PREEMPTED, correlation_id=correlation_id)

    async def _run(
        self, gesture: Gesture, legs: tuple[Leg, ...], *, correlation_id: UUID
    ) -> None:
        """Walk *legs* onto the wheels — homing first, then the plan — bracketed by events.

        ``duration_ms`` on the completed event is measured with ``monotonic_ns`` (§9.1.1), and
        ``net_mm`` is the **plan's** own arithmetic (``0.0`` for every plan the domain emits),
        so a completed step whose payload says otherwise is visibly a defect.
        """
        started_ns = self._clock.monotonic_ns()
        caps = self._drive.capabilities
        assert caps is not None  # a plan exists only when the rig has wheels
        homing = homing_leg(self._offset_mm, caps, self._geometry)
        if homing:
            _log.info(
                "homing %.1f mm before %s [correlation_id=%s]",
                self._offset_mm,
                gesture.name.lower(),
                correlation_id,
            )
        out = next(leg for leg in legs if leg.heading is not None)
        await self._bus.publish(
            DriveStepStarted(
                **envelope(
                    clock=self._clock, correlation_id=correlation_id, source=_SOURCE
                ),
                gesture=gesture.name.lower(),
                heading=cast(Heading, out.heading).name.lower(),
                distance_mm=abs(out.distance_mm),
            )
        )
        try:
            for leg in (*homing, *legs):
                await self._run_leg(leg)
        except asyncio.CancelledError:
            raise  # a preemption or a shutdown — reported by whoever cancelled
        except _EdgeSeen as seen:
            await self._on_edge(gesture, seen.leg, correlation_id=correlation_id)
            return
        except _OverBudget:
            self._forget_if_current()
            await self._publish_aborted(gesture, BUDGET, correlation_id=correlation_id)
            return
        except Exception as exc:  # noqa: BLE001 — §3.12.3: abort, stop, publish, CONTINUE
            await self._abort(gesture, exc, correlation_id=correlation_id)
            return
        elapsed_ms = int((self._clock.monotonic_ns() - started_ns) / _NS_PER_MS)
        self.steps_performed += 1
        self._forget_if_current()
        await self._bus.publish(
            DriveStepCompleted(
                **envelope(
                    clock=self._clock, correlation_id=correlation_id, source=_SOURCE
                ),
                gesture=gesture.name.lower(),
                duration_ms=elapsed_ms,
                net_mm=net_mm(legs),
            )
        )

    async def _run_leg(self, leg: Leg, *, watch: bool = True) -> None:
        """Run one leg, watching the sensors, and account for what was actually driven.

        A dwell is a wait. A moving leg is: the budget guard, one ``clear()`` before the wheels
        turn, then ``Drive.run`` in a task polled every ``poll_ms`` — an edge mid-leg stops the
        motors **through the port** and raises :class:`_EdgeSeen` for the caller to retreat on.
        Cancellation stops the motors too, in ``finally``-shaped handling, so a preempted or
        shut-down leg never leaves a wheel turning.

        The odometer advances by the fraction of the leg that ran: ``distance × driven /
        planned``. That is the whole reason ``run()`` returns milliseconds driven.
        """
        if leg.heading is None:
            await asyncio.sleep(leg.duration_ms / 1000)
            return
        # The loud guard. A leg that would end further from origin than the budget AND further
        # than the robot already is — so a homing leg from beyond the budget is still allowed,
        # because it brings the robot closer; refusing it would strand the robot out there.
        after = self._offset_mm + leg.distance_mm
        if abs(after) > self._geometry.max_excursion_mm + 1e-9 and abs(after) > abs(
            self._offset_mm
        ):
            raise _OverBudget
        if watch and not await self._edge.clear():
            raise _EdgeSeen(leg)
        left, right = leg.wheels
        # ``create_task`` rather than ``spawn``: the run has a RESULT — the milliseconds driven —
        # and ``spawn`` is for fire-and-forget coroutines whose result is nobody's business.
        run = asyncio.create_task(
            self._drive.run(left, right, duration_ms=leg.duration_ms),
            name="DriveService.leg",
        )
        edge_seen = False
        try:
            while not run.done():
                done, _ = await asyncio.wait({run}, timeout=self._poll_s)
                if done or not watch:
                    continue
                if not await self._edge.clear():
                    edge_seen = True
                    await self._drive.stop()
                    break
            driven_ms = await run
        except asyncio.CancelledError:
            # A preemption or a shutdown. Stop the motors THROUGH THE PORT first — the run then
            # ends at its next slice and reports what it drove — and account for that before
            # re-raising, so the odometer stays honest about a leg that was cut. Cancelling the
            # run outright would throw its return value away, and with it the offset the next
            # step needs to home from.
            await self._stop_motors()
            self._account(leg, await self._collect(run))
            raise
        self._account(leg, driven_ms)
        if edge_seen:
            raise _EdgeSeen(leg)

    def _account(self, leg: Leg, driven_ms: int) -> None:
        """Advance the odometer by the fraction of *leg* that actually ran."""
        self._offset_mm += leg.distance_mm * driven_ms / leg.duration_ms

    @staticmethod
    async def _collect(run: asyncio.Task[int]) -> int:
        """The milliseconds a stopped run reports — ``0`` if it cannot be collected promptly.

        Bounded, because this runs on a cancellation path that must not hang: a real adapter
        ends a run within one slice of a ``stop()``, and one that does not is a fault whose
        distance is unknowable anyway. ``asyncio.wait`` rather than ``wait_for``, because
        ``wait_for`` cancels what it waits on when its caller is cancelled — and its caller
        here *is* a task being cancelled — which would throw the driven figure away.
        """
        done, _ = await asyncio.wait({run}, timeout=1.0)
        if not done:
            run.cancel()
            return 0
        try:
            return run.result()
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - nothing knowable was driven
            return 0

    async def _on_edge(
        self, gesture: Gesture, leg: Leg, *, correlation_id: UUID
    ) -> None:
        """The sensors earned their place: stop, retreat if the leg was heading at the edge,
        latch, report.

        The retreat is the homing leg — back to origin by the distance this service believes it
        is out — run **unwatched**, because the sensors face forward and the robot is now
        moving away from what they saw.
        Only a *forward* leg retreats: an edge reading at the front while reversing means the
        robot is already moving away from it, and driving forward again would be driving into
        it. The offset is left honest either way; the next step homes.
        """
        self._edge_latched = True
        _log.warning(
            "edge under the sensors during %s (%s) — stopped [correlation_id=%s]",
            gesture.name.lower(),
            cast(Heading, leg.heading).name.lower(),
            correlation_id,
        )
        if leg.heading is Heading.FORWARD and self._drive.capabilities is not None:
            retreat = homing_leg(
                self._offset_mm, self._drive.capabilities, self._geometry
            )
            for back in retreat:
                if back.heading is Heading.BACKWARD:
                    with contextlib.suppress(_EdgeSeen, _OverBudget):
                        await self._run_leg(back, watch=False)
        self._forget_if_current()
        await self._publish_aborted(gesture, EDGE, correlation_id=correlation_id)

    async def _abort(
        self, gesture: Gesture, exc: BaseException, *, correlation_id: UUID
    ) -> None:
        """§3.12.3's fault path: stop the motors, report, and stay live for the next step.

        *Nothing except a bad API key at boot is allowed to stop the robot.* A wheel that faults
        must not take down a conversation — and must not keep turning either, so the stop comes
        first and is unconditional.
        """
        self._forget_if_current()
        _log.error(
            "step %s aborted by a device fault; motors stopped [correlation_id=%s]",
            gesture.name.lower(),
            correlation_id,
            exc_info=exc,
        )
        await self._stop_motors()
        await self._publish_aborted(gesture, FAULT, correlation_id=correlation_id)

    async def _publish_aborted(
        self, gesture: Gesture, reason: str, *, correlation_id: UUID
    ) -> None:
        self.steps_aborted[reason] += 1
        await self._bus.publish(
            DriveStepAborted(
                **envelope(
                    clock=self._clock, correlation_id=correlation_id, source=_SOURCE
                ),
                gesture=gesture.name.lower(),
                reason=reason,
            )
        )

    # --- the edge latch ------------------------------------------------------------------

    async def _edge_has_cleared(self) -> bool:
        """Whether ``clear()`` has held for ``clear_hold_ms`` — polled on the injected clock.

        A single clear reading is not enough: a hand lifts for a moment and comes back. The hold
        is what turns "the sensor said clear once" into "the desk is there". Releases the latch
        on success; leaves it set otherwise.
        """
        polls = max(1, round(self._clear_hold_s / self._poll_s))
        for _ in range(polls):
            if not await self._edge.clear():
                return False
            await self._clock.sleep(self._poll_s)
        self._edge_latched = False
        return True

    # --- idle steps: the only trigger in v1 ----------------------------------------------

    async def _idle_steps(self) -> None:
        """Step at irregular intervals, forever, while IDLE and nothing else is moving.

        The band is randomised for the reason ``MotionService``'s drift band is — a metronome
        reads as a mechanism — and it is an order of magnitude rarer, because a gearbox is
        audible where a servo drift is not. It does not queue: an idle step deferred by a
        conversation is a step nobody wanted.
        """
        while True:
            await self._clock.sleep(
                self._rng.uniform(self._idle_min_s, self._idle_max_s)
            )
            if not self._idle or self._task is not None or self._edge_latched:
                continue
            gesture, self._next_idle = (
                self._next_idle,
                (
                    Gesture.STEP_BACK
                    if self._next_idle is Gesture.STEP_TOWARD
                    else Gesture.STEP_TOWARD
                ),
            )
            await self.perform(gesture, correlation_id=uuid4())

    # --- housekeeping --------------------------------------------------------------------

    @property
    def offset_mm(self) -> float:
        """Where the service believes the robot is, mm from origin (introspection)."""
        return self._offset_mm

    @property
    def edge_latched(self) -> bool:
        """Whether an edge abort has locked the wheels pending a clear hold (introspection)."""
        return self._edge_latched

    async def _stop_motors(self) -> None:
        """Cut the motors. Never raises — this runs on teardown paths."""
        try:
            await self._drive.stop()
        except Exception:  # noqa: BLE001 - a failed stop must not mask the reason we stopped
            _log.warning("drive stop failed", exc_info=True)

    def _forget_if_current(self) -> None:
        """Drop the in-flight handles, but only if they are still *this* step's — the same
        guard ``MotionService`` carries, for the same race."""
        if self._task is asyncio.current_task():
            self._task = None
            self._current = None


class _OverBudget(Exception):
    """The next leg would take the odometer past the budget. Should never fire; loud if it does."""


__all__ = ["DriveService"]
