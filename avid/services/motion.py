"""The mover — one affect onto servos, one gesture at a time (#203, SDS §3.6.1).

The other arm of §3.7.2's fan-out. ``AffectService`` decides which affect is current and
publishes ``affect.changed``; ``ExpressionService`` draws it; this service *moves* to it. The
three know nothing about each other, which is the property the event bus exists for: one
publish, the face changes **and** the servo nods, and ``ConversationService`` never learned
that a servo exists.

So this owns the :class:`~avid.core.ports.Servo` port and is the only thing in the system that
calls ``move_to``/``relax`` (SDS §9.1.4) — a test asserts as much, mirroring
``ExpressionService``'s "only caller of ``render()``".

**The one structural difference from ``ExpressionService``, and it is the whole difficulty: a
render is instantaneous, a gesture takes time.** Drawing a face is one awaited call that either
happened or did not. A nod is nearly a second of physical motion that can be interrupted,
can fault halfway, and leaves the rig somewhere when it stops. That turns a reactive handler
into an owned *task*, which is why this service is returned to the lifecycle for
``start``/``stop`` like ``AudioService`` — and it is where preemption, fault handling and the
relax timer all come from.

**What this service does not do.** It does not re-clamp: the safe reach is a property of the
physical linkage and is enforced at the lowest layer that can enforce it universally
(§3.9.1), so a second check here would be a second place to be wrong and would mask a bad
``[servo]`` reach that #207's bring-up needs to surface. It does not decide *which* gesture —
that is ``gesture_for`` (#202) — and it does not decide what a gesture *looks like* on this
rig, which is ``plan()`` (#201). What is left is scheduling, and scheduling is genuinely all
that is left.

**Capability negotiation reads the adapter, never config** (§3.9.3). The axes come from
``Servo.axes``, so the planner is handed the rig that is actually attached rather than a list
some file claims. ``[motion] axes`` used to be that list; #200 deleted it rather than
reconciling it, because two copies of one fact is drift and drift in config is silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import cast
from uuid import UUID

from avid.core.affect_map import gesture_for
from avid.core.envelope import envelope
from avid.core.event_bus import (
    DEFAULT_MAXSIZE,
    Handler,
    OverflowPolicy,
    Subscription,
)
from avid.core.ports import Clock, EventBus, Servo
from avid.core.tasks import spawn
from avid.domain import (
    AffectChanged,
    Axis,
    Direction,
    Event,
    Gesture,
    Keyframe,
    LookAtResult,
    MotionGestureCompleted,
    MotionGesturePreempted,
    MotionGestureStarted,
    RobotState,
    StateTransitioned,
    gesture_for_direction,
    plan,
)

_log = logging.getLogger(__name__)

_SOURCE = "MotionService"

_NS_PER_MS = 1_000_000


class MotionService:
    """Turns one affect into servo movement, safely (SDS §3.6.1).

    Shaped to SDS §9.2 (``name`` / ``start`` / ``stop`` / ``subscriptions``) and depends on the
    :class:`~avid.core.ports.Servo`, :class:`~avid.core.ports.EventBus` and
    :class:`~avid.core.ports.Clock` **Protocols**, never a concrete adapter (P2) — which is what
    lets the identical service drive a recorded trace in CI and a PCA9685 on the bench.
    """

    name = _SOURCE

    def __init__(
        self,
        *,
        bus: EventBus,
        servo: Servo,
        clock: Clock,
        idle_relax_ms: int,
        look_at_cooldown_ms: int,
    ) -> None:
        self._bus = bus
        self._servo = servo
        self._clock = clock
        self._idle_relax_s = idle_relax_ms / 1000
        self._look_at_cooldown_ns = look_at_cooldown_ms * _NS_PER_MS

        # Axis name -> channel, resolved once from the rig's own report. The domain addresses
        # an axis by name (a Keyframe says "tilt"); the port is keyed by channel. This dict is
        # the entire translation, and it is the only place in the service that knows a channel
        # is a number rather than a name.
        self._channels = {axis.name: axis.channel for axis in servo.axes}

        # The gesture in flight, and what it is. Held together because preemption needs both:
        # the task to cancel, and the name to put in the event that reports the cancellation.
        self._task: asyncio.Task[None] | None = None
        self._current: Gesture | None = None

        # The relax countdown. A separate task from the gesture because it outlives it: the
        # timer starts when a gesture *ends* and its whole job is to still be there some
        # seconds later, when nothing else is.
        self._relax_task: asyncio.Task[None] | None = None

        # When the last look_at was ACCEPTED — the cooldown's origin. Only accepted calls move
        # it: a rejected one extending its own cooldown would lock a politely-retrying model out
        # for as long as it kept asking.
        self._last_look_ns: int | None = None

        # The monotonic stamp of the newest event acted on — the same staleness guard
        # ``ExpressionService`` carries, and for the same reason. This service reads two
        # queues and the bus guarantees FIFO *per subscriber*, not across them (SDS §3.5), so
        # their relative order is a race. Monotonic, never wall clock (§9.1.1): an NTP step
        # would otherwise make every later event look stale and freeze the robot permanently.
        self._last_acted_ns: int | None = None

        # Counters an operator reads, not facts anyone is notified of. Publishing them would be
        # the "effect as fact" mistake ``ExpressionService``'s docstring rules out.
        self.gestures_performed = 0
        self.gestures_preempted = 0
        self.gestures_aborted = 0
        self.stale_skipped = 0

    # --- SDS §9.2 service shape ----------------------------------------------------------

    async def start(self) -> None:
        """Nothing to launch — the gesture task is spawned on demand, not at boot.

        Deliberately empty rather than absent. A robot that gestured on ``start()`` would move
        before it had anything to express, and the first thing anyone should see a servo do is
        nothing.
        """

    async def stop(self) -> None:
        """Cancel any gesture in flight and leave every channel de-energised. Idempotent.

        ⚠️ **Relaxing on the way out is the one failure mode that outlives the process.**
        Everything else this service can get wrong stops when the robot stops; a channel left
        holding torque keeps drawing current and humming after the program is gone, until
        somebody pulls the plug. So the relax is unconditional and happens even if the cancel
        raised.

        Completes well inside §9.2's 5 s budget: one cancel, one await of an already-cancelled
        task, and one ``relax`` per axis.
        """
        self._cancel_relax_timer()
        task, self._task = self._task, None
        self._current = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._relax_all()

    def subscriptions(self) -> Sequence[Subscription]:
        """Declare, do not register (SDS §9.2).

        ``affect.changed`` is the gesture path — the §3.7.2 arrow this service exists to close.

        ``state.transitioned`` is read **independently**, the way ``ExpressionService`` reads it
        for the Tier-1 baseline: a service that needs to know the robot fell asleep can read the
        operational state for itself rather than being told by another service. Neither knows
        the other subscribes, which is P5 working as intended.

        DROP_OLDEST on both (SDS §9.1.3): a backlog of stale gestures is worse than no backlog,
        because performing them in order means the robot is expressing something it stopped
        feeling several seconds ago. The newest intent is the only one worth performing.
        """
        return (
            Subscription(
                event_type=AffectChanged,
                handler=cast(Handler, self._on_affect_changed),
                name="MotionService.affect_changed",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=StateTransitioned,
                handler=cast(Handler, self._on_state_transitioned),
                name="MotionService.state_transitioned",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
        )

    # --- the gesture path ----------------------------------------------------------------

    async def _on_affect_changed(self, event: AffectChanged) -> None:
        """Affect in, gesture out — or, most of the time, nothing (§3.7.2, #202)."""
        if self._is_stale(event):
            return
        gesture = gesture_for(event.affect)
        if gesture is None:
            return
        await self.perform(gesture, correlation_id=event.correlation_id)

    async def _on_state_transitioned(self, event: StateTransitioned) -> None:
        """Sleep relaxes **immediately**; every other state is the timer's business.

        ⚠️ Deliberately not routed through the idle timer. A robot that has just fallen asleep
        is a robot nobody is looking at, and waiting out ``idle_relax_ms`` there means humming
        into an empty room for the one interval where it is most obviously wrong. It also
        cancels any gesture in flight first — a nod that finishes after the robot is asleep is a
        contradiction someone will eventually watch happen.

        No staleness guard here, and that is on purpose: relaxing is idempotent and harmless,
        so a late ``SLEEPING`` costs nothing, while *skipping* one would leave the rig
        energised for the whole nap.
        """
        if event.to is not RobotState.SLEEPING:
            return
        self._cancel_relax_timer()
        await self._preempt(by=None, correlation_id=event.correlation_id)
        await self._relax_all()

    def _is_stale(self, event: Event) -> bool:
        """Whether *event* has been overtaken by one this service already acted on.

        Not an optimisation. Acting last-writer-wins across two queues would leave the robot
        performing an affect the system has already moved past — and unlike a face, a gesture
        that arrives late is not merely wrong for a moment, it is a servo moving for no reason
        anyone watching can account for.
        """
        if self._last_acted_ns is not None and event.monotonic_ns < self._last_acted_ns:
            self.stale_skipped += 1
            _log.debug(
                "skipped superseded gesture trigger [correlation_id=%s]",
                event.correlation_id,
            )
            return True
        self._last_acted_ns = event.monotonic_ns
        return False

    async def perform(self, gesture: Gesture, *, correlation_id: UUID) -> None:
        """Start *gesture*, preempting whatever is running. Returns once it is **armed**.

        **It does not await the movement**, and that is the load-bearing part. The caller is a
        bus handler or (at #204) a tool dispatch, and neither may sit for the second a nod
        takes — §6.6 classifies the motion tool fire-and-forget for exactly this reason, and a
        handler that blocked would apply backpressure to the queue behind it. So this arms a
        task and returns; the movement happens in it.

        It *is* a coroutine, because arming involves publishing ``motion.gesture_preempted``
        and the ordering matters: awaiting that publish is what guarantees the preemption is
        queued **before** the new gesture's ``gesture_started``. Spawning it instead would let
        a log read as though the new gesture began before the old one was cut, which is
        precisely the confusion #207's AC-3 has to rule out by eye.

        An empty plan is a **no-op**, not an error (#201 AC-5): a rig that cannot express a
        gesture should be quiet, not broken, and nothing is published — no ``gesture_started``
        for a gesture that never started.
        """
        keyframes = plan(gesture, self._servo.axes)
        if not keyframes:
            _log.debug(
                "rig cannot express %s — no-op [correlation_id=%s]",
                gesture.name.lower(),
                correlation_id,
            )
            return
        # A gesture starting is the end of being idle. Cancel before arming anything else, so
        # a timer that came due while this was being set up cannot relax underneath the sweep.
        self._cancel_relax_timer()
        await self._preempt(by=gesture, correlation_id=correlation_id)
        self._current = gesture
        self._task = spawn(
            self._run(gesture, keyframes, correlation_id=correlation_id),
            name=f"MotionService.{gesture.name.lower()}",
        )

    async def _preempt(self, *, by: Gesture | None, correlation_id: UUID) -> None:
        """Cancel the gesture in flight, if any, and report that it was cut short.

        **Synchronous cancel**, copied from ``PresenceService._cancel_nap``: the caller must not
        stall, and ``spawn``'s done-callback treats ``CancelledError`` as a clean teardown
        rather than a reported death. What this adds over the nap is the publish — a preempted
        gesture is a *fact* about the rig that ``motion.gesture_preempted`` exists to carry.

        ``by`` distinguishes the two causes §9.1.3 gives that event: a gesture name when a newer
        gesture interrupted, ``None`` for §3.12.3's fault abort.
        """
        task, self._task = self._task, None
        cancelled, self._current = self._current, None
        if task is None or task.done() or cancelled is None:
            return
        task.cancel()
        self.gestures_preempted += 1
        await self._bus.publish(
            MotionGesturePreempted(
                **envelope(
                    clock=self._clock, correlation_id=correlation_id, source=_SOURCE
                ),
                gesture=cancelled.name.lower(),
                by=None if by is None else by.name.lower(),
            )
        )

    async def _run(
        self, gesture: Gesture, keyframes: tuple[Keyframe, ...], *, correlation_id: UUID
    ) -> None:
        """Walk *keyframes* onto the rig, bracketed by ``gesture_started``/``_completed``.

        The duration reported is **measured, not planned**, and measured with ``monotonic_ns``
        (§9.1.1) — a wall-clock step yields a negative duration and poisons the one metric this
        milestone is graded on. A measured figure that drifts from ``plan()``'s prediction is a
        loop under load, which is worth being able to see.
        """
        started_ns = self._clock.monotonic_ns()
        moved = self._axes_named({frame.axis for frame in keyframes})
        await self._bus.publish(
            MotionGestureStarted(
                **envelope(
                    clock=self._clock, correlation_id=correlation_id, source=_SOURCE
                ),
                gesture=gesture.name.lower(),
                axes=moved,
            )
        )
        try:
            for frame in keyframes:
                await self._servo.move_to(
                    self._channels[frame.axis],
                    frame.angle_deg,
                    duration_ms=frame.duration_ms,
                )
        except asyncio.CancelledError:
            raise  # a preemption, already reported by the preempting caller
        except Exception as exc:  # noqa: BLE001 — §3.12.3: abort, relax, publish, CONTINUE
            await self._abort(gesture, exc, correlation_id=correlation_id)
            return
        elapsed_ms = int((self._clock.monotonic_ns() - started_ns) / _NS_PER_MS)
        self.gestures_performed += 1
        self._forget_if_current()
        # The rig is now holding whatever angle the gesture ended on. Arm the countdown that
        # lets go of it — this is the *only* place a normal relax is scheduled from, so the
        # timer can never be running while a gesture is.
        self._arm_relax_timer(correlation_id=correlation_id)
        await self._bus.publish(
            MotionGestureCompleted(
                **envelope(
                    clock=self._clock, correlation_id=correlation_id, source=_SOURCE
                ),
                gesture=gesture.name.lower(),
                duration_ms=elapsed_ms,
            )
        )

    async def _abort(
        self, gesture: Gesture, exc: BaseException, *, correlation_id: UUID
    ) -> None:
        """§3.12.3's I²C-fault path, which the SDS states as a fact rather than a suggestion:
        *"Abort gesture, relax servo, publish ``motion.gesture_preempted``, continue."*

        The through-line of §3.12.3 is that **nothing except a bad API key at boot is allowed
        to stop the robot**. A servo fault must not take down a conversation — so this reports,
        tidies up, and leaves the service live for the next gesture. A test drives a fake that
        raises mid-sweep and then asserts the *next* gesture still runs, because "it did not
        crash" and "it still works" are different claims.

        ⚠️ **Every channel is relaxed, not just the one that faulted, and the reason is worth
        writing down** (the issue asks for the decision either way). Two arguments, both
        pointing the same way: a half-completed gesture leaves the head at an arbitrary angle
        rather than a resting one, and a rig that has just failed an I²C write is not a rig
        anyone should trust to keep holding torque. The cost of being wrong in this direction
        is a robot that goes limp; in the other, it is a stalled servo on a browning-out rail
        (R-04), which is the failure this milestone has a whole spike for.

        ``by=None`` is what distinguishes this from an ordinary preemption on the shared
        catalog row (§9.1.3), and ``ObservabilityService`` logs it at WARNING for the same
        reason.
        """
        self.gestures_aborted += 1
        self._forget_if_current()
        _log.error(
            "gesture %s aborted by a device fault; relaxing every channel "
            "[correlation_id=%s]",
            gesture.name.lower(),
            correlation_id,
            exc_info=exc,
        )
        await self._relax_all()
        await self._bus.publish(
            MotionGesturePreempted(
                **envelope(
                    clock=self._clock, correlation_id=correlation_id, source=_SOURCE
                ),
                gesture=gesture.name.lower(),
                by=None,
            )
        )

    # --- the GestureTools port (#204, SDS §6.6, §3.9.1) ----------------------------------

    async def look_at(
        self, direction: Direction, *, correlation_id: UUID
    ) -> LookAtResult:
        """Point the robot *direction* — the model's one motion tool (§6.6).

        Satisfies :class:`~avid.core.ports.GestureTools` **structurally**: nothing here inherits
        from it, nothing in this module imports ``ai``, and the composition root injects this
        service into ``ConversationService`` as the port. That is the whole mechanism by which
        the model can move a servo without the conversation layer knowing a servo exists.

        Returns as soon as the gesture is **accepted**, never after the sweep — §6.6 classifies
        this fire-and-forget because awaiting the better part of a second before returning
        ``function_call_output`` stalls the turn and makes step 5's ``response.create`` land as
        audible dead air.

        Two ways a well-formed request is declined, and the model is told which:

        ⚠️ **The cooldown is a safety property, not a nicety.** A model that decides gesturing is
        delightful would otherwise drive both servos continuously — which contradicts the gate's
        relax clause by construction and puts sustained load on the rail #206 is measuring. Note
        it advances **only on acceptance**: a rejected call must not extend its own cooldown, or
        a model retrying politely would lock itself out for as long as it kept asking.

        And a direction this rig has no axis for is declined **honestly** rather than performed
        as a no-op. A silent success would leave the model believing it moved, and a robot that
        describes motion that never happened is worse than one that says it cannot — the same
        argument §7.6 makes about memory, in a different organ.

        There is deliberately **no** *"decline while SPEAKING"* rule (the issue asks for the
        decision either way). Gesturing mid-sentence is what people do, the cooldown already
        bounds the rate, and a robot that will not look at you while it is talking is a robot
        that will not look at you during most of the conversation.
        """
        remaining_ns = self._cooldown_remaining_ns()
        if remaining_ns > 0:
            _log.debug(
                "look_at(%s) declined: %d ms of cooldown left [correlation_id=%s]",
                direction.name.lower(),
                remaining_ns // _NS_PER_MS,
                correlation_id,
            )
            return LookAtResult.COOLING_DOWN

        gesture = gesture_for_direction(direction)
        if not plan(gesture, self._servo.axes):
            _log.info(
                "look_at(%s) declined: this rig has no axis for it [correlation_id=%s]",
                direction.name.lower(),
                correlation_id,
            )
            return LookAtResult.NO_AXIS

        self._last_look_ns = self._clock.monotonic_ns()
        await self.perform(gesture, correlation_id=correlation_id)
        return LookAtResult.ACCEPTED

    def _cooldown_remaining_ns(self) -> int:
        """Nanoseconds left before another ``look_at`` may be accepted; ``0`` if it may now.

        Monotonic, never wall clock (§9.1.1). A cooldown measured on ``timestamp_ms`` would be
        skipped entirely by an NTP step forward — and the Pi corrects its clock seconds after
        boot, which is exactly when a first conversation is likely to be happening.
        """
        if self._last_look_ns is None:
            return 0
        elapsed = self._clock.monotonic_ns() - self._last_look_ns
        return max(0, self._look_at_cooldown_ns - elapsed)

    # --- relaxing when idle — the gate's "no buzz" clause ---------------------------------

    def _arm_relax_timer(self, *, correlation_id: UUID) -> None:
        """Start the countdown to a de-energised rig, replacing any pending one.

        An SG90 holding position buzzes audibly and warms, and **an idle robot that hums is a
        robot that gets unplugged** — which is the whole of the gate's fourth clause, graded by
        ear in a quiet room rather than by any assertion here.

        The timer sleeps on the injected ``Clock``, so a test drives it in microseconds rather
        than waiting out ``idle_relax_ms``. That is the same reason ``Clock`` is a port at all
        (SDS §9.3): the alternative is a suite nobody runs.
        """
        self._cancel_relax_timer()
        self._relax_task = spawn(
            self._relax_when_idle(correlation_id=correlation_id),
            name="MotionService.relax_timer",
        )

    def _cancel_relax_timer(self) -> None:
        """Drop a pending countdown. Synchronous — callers are handlers that must not stall.

        ``spawn``'s done-callback ignores ``CancelledError``, so a cancelled timer is a clean
        teardown rather than a reported death (``PresenceService._cancel_nap``, same shape).
        """
        task, self._relax_task = self._relax_task, None
        if task is not None and not task.done():
            task.cancel()

    async def _relax_when_idle(self, *, correlation_id: UUID) -> None:
        """Wait out the idle window, then let go of every channel.

        ⚠️ **Built so #205 configures it rather than fights it.** Idle micro-motion and
        "relaxes when idle" are in direct tension — a servo that drifts every few seconds is
        never idle long enough to relax. Keeping the countdown a plain re-armable task means
        the drift path can simply *be* a gesture that re-arms it, instead of needing this
        logic changed or bypassed.
        """
        await self._clock.sleep(self._idle_relax_s)
        _log.debug(
            "idle for %.1fs — relaxing [correlation_id=%s]",
            self._idle_relax_s,
            correlation_id,
        )
        await self._relax_all()

    # --- the rig -------------------------------------------------------------------------

    def _axes_named(self, names: set[str]) -> tuple[Axis, ...]:
        """The rig's axes this performance actually drives, in the rig's own order.

        The negotiated result rather than the inventory: on a rig with no tilt a nod publishes
        ``pan``, so one log line separates a real tilt nod from the fallback without anyone
        re-deriving it from the plan.
        """
        return tuple(axis for axis in self._servo.axes if axis.name in names)

    async def _relax_all(self) -> None:
        """De-energise every channel. Never raises — this runs on teardown paths."""
        for axis in self._servo.axes:
            try:
                await self._servo.relax(axis.channel)
            except Exception:  # noqa: BLE001 - a failed relax must not mask the reason we relaxed
                _log.warning(
                    "relax failed on channel %d (%s)",
                    axis.channel,
                    axis.name,
                    exc_info=True,
                )

    def _forget_if_current(self) -> None:
        """Drop the in-flight handles, but only if they are still *this* gesture's.

        ⚠️ A completing gesture must not clear a newer one's handles. Preemption cancels the
        old task and arms the new one immediately; the cancelled coroutine then runs its
        remaining lines, and an unguarded ``self._task = None`` there would leave the service
        believing nothing is in flight while a servo is mid-sweep — so the next preemption
        would publish nothing and ``stop()`` would leave the rig energised.
        """
        if self._task is asyncio.current_task():
            self._task = None
            self._current = None
