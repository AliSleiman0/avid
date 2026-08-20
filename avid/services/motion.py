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

**Idle micro-motion and "relaxes when idle" are in direct tension, and the resolution is
option (b)** (#205 AC-2). A robot that holds perfectly still between utterances reads as
switched off; a servo that drifts every few seconds is never idle long enough to relax. So
**each drift re-energises, moves, and relaxes immediately after** — the channel carries a pulse
for a few hundred milliseconds per drift rather than continuously, which satisfies both of the
gate's clauses literally instead of weakening either. The alternatives were rejected for
reasons worth keeping: bounding the drift to a window after activity (a) makes the robot go
*more* still the longer nobody talks to it, which is backwards; suppressing drift once relaxed
(c) collapses to no micro-motion at all within one idle window, which is the feature not
existing. A test asserts the rig is de-energised for the **majority** of an idle window,
because that is the actual claim.

**Capability negotiation reads the adapter, never config** (§3.9.3). The axes come from
``Servo.axes``, so the planner is handed the rig that is actually attached rather than a list
some file claims. ``[motion] axes`` used to be that list; #200 deleted it rather than
reconciling it, because two copies of one fact is drift and drift in config is silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
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

# How long one idle drift takes. Slow enough to read as breathing rather than as a twitch, and
# slow enough that the first drift after a `look_at` walks the head back toward centre instead
# of snapping it.
_DRIFT_MS = 700


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
        drift_interval_min_s: float,
        drift_interval_max_s: float,
        drift_amplitude_frac: float,
        rng: random.Random | None = None,
    ) -> None:
        self._bus = bus
        self._servo = servo
        self._clock = clock
        self._idle_relax_s = idle_relax_ms / 1000
        self._look_at_cooldown_ns = look_at_cooldown_ms * _NS_PER_MS
        self._drift_min_s = drift_interval_min_s
        self._drift_max_s = drift_interval_max_s
        self._drift_amplitude = drift_amplitude_frac
        # Injectable only so a test can seed it. ``main`` passes nothing; the drift is meant to
        # be unpredictable in the room and reproducible in the suite, which are not in tension
        # as long as the seam is this narrow.
        self._rng = rng if rng is not None else random.Random()

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

        # The idle-drift scheduler (#205). Long-lived, unlike the drift movements it arms.
        self._drift_task: asyncio.Task[None] | None = None
        # Whether the robot is asleep. Micro-motion never runs then (AC-3) — a sleeping robot
        # that twitches is a robot that did not go to sleep.
        self._asleep = False

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
        """Launch the idle-drift scheduler. Idempotent.

        Gestures are still spawned on demand rather than here — a robot that *gestured* on
        ``start()`` would be expressing something before it had anything to express, and the
        first thing anyone should see a servo do is nothing. The drift loop is different: its
        first move is a random interval away, so booting it here costs nothing and means an
        unattended robot reads as alive without anyone having spoken to it.
        """
        if self._drift_task is None:
            self._drift_task = spawn(
                self._micro_motion(), name="MotionService.micro_motion"
            )

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
        drift, self._drift_task = self._drift_task, None
        if drift is not None and not drift.done():
            drift.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drift
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
        self._asleep = event.to is RobotState.SLEEPING
        if not self._asleep:
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
        if task is None or task.done():
            return
        task.cancel()
        if cancelled is None:
            # An idle drift, not a gesture (#205 AC-3). Cancelled just the same — there is only
            # ever one thing moving the rig — but nothing is published: a drift is decoration,
            # and reporting hundreds of them an hour would make motion.* useless for debugging
            # the events that matter.
            return
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

    # --- idle micro-motion, and the relax tension it creates (#205) -----------------------
    #
    # A robot that holds perfectly still between utterances reads as *switched off*. Small
    # occasional drift is most of the perceived difference between a prop and a companion, and
    # it is cheap.
    #
    # ⚠️ **It fights the relax clause, and the resolution is deliberate.** The gate asks for
    # "idle micro-motion" *and* "servo relaxes when idle (no buzz)", and a servo that drifts
    # every few seconds is never idle long enough to relax — or worse, is re-energised by the
    # drift and then hums continuously between drifts. #205 lists three ways out; this is (b),
    # and it satisfies both clauses **literally** rather than by weakening either:
    #
    #     each drift re-energises, moves, and relaxes immediately after.
    #
    # So the channel carries a pulse for a few hundred milliseconds per drift instead of
    # continuously, and the rig is de-energised for the overwhelming majority of any idle
    # window — which a test asserts, because "the majority" is the actual claim and a vaguer
    # one would be satisfied by a robot that hums half the time.
    #
    # (a) — drift for a bounded window then stop — was rejected because it makes the robot go
    # *more* still the longer nobody talks to it, which is backwards. (c) — suppress drift once
    # relaxed — collapses to "no micro-motion at all" within one idle window, which is the
    # feature not existing.

    async def _micro_motion(self) -> None:
        """Drift at irregular intervals, forever, while the service is running.

        **The interval is randomised and that is a design requirement, not a flourish** (AC-4).
        A perfectly periodic twitch reads as a *mechanism*, which is worse than stillness: the
        eye picks up the rhythm in seconds and the illusion inverts. Randomness lives here in
        the service rather than in :func:`~avid.domain.motion.plan`, which must stay pure —
        a random planner would make every gesture assertion in the suite a flake, and the
        failures would look like hardware.
        """
        while True:
            await self._clock.sleep(self._drift_interval_s())
            if self._asleep or self._task is not None:
                # Yields to everything: a real gesture in flight, or a sleeping robot. It does
                # not queue behind them either — a drift deferred is a drift nobody wanted.
                continue
            self._drift()

    def _drift_interval_s(self) -> float:
        """A random wait inside the configured band."""
        return self._rng.uniform(self._drift_min_s, self._drift_max_s)

    def _drift(self) -> None:
        """Arm one small movement, through the same slot a gesture uses.

        Sharing the slot is what makes AC-3 true by construction rather than by a check: a real
        gesture arriving mid-drift cancels it, exactly as it would cancel another gesture,
        because there is only ever one thing moving the rig.

        ⚠️ **It publishes nothing.** A drift is not a gesture, and filling the ``motion.*``
        catalog with hundreds of them per hour would make those events useless for debugging the
        ones that matter — which is what #207 reads them for. ``self._current`` stays ``None``,
        which is precisely what tells :meth:`_preempt` there is no *gesture* to report.
        """
        self._task = spawn(self._drift_once(), name="MotionService.drift")

    async def _drift_once(self) -> None:
        """Move one axis slightly off centre, then let go of it immediately.

        Amplitude is a fraction of the axis's **declared reach**, never absolute degrees (AC-1):
        a few degrees on a wide pan and a few degrees on a narrow tilt are not the same gesture.
        Keep it smaller than instinct suggests — micro-motion that is *noticeable* is a tic;
        micro-motion that is only noticeable by its absence is the goal.

        Relative to **centre** rather than to wherever the last gesture finished, which also
        means an idle robot slowly settles back to its resting pose instead of sitting at
        whatever angle a ``look_at`` left it. That is intended: it is the behaviour that makes
        an unattended robot look composed rather than abandoned mid-thought.
        """
        axis = self._rng.choice(self._servo.axes)
        offset = self._rng.uniform(-self._drift_amplitude, self._drift_amplitude)
        angle = axis.centre_deg + offset * axis.half_span_deg
        try:
            await self._servo.move_to(axis.channel, angle, duration_ms=_DRIFT_MS)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a drift is decoration; it must never be a fault
            _log.warning("idle drift failed on %s", axis.name, exc_info=True)
        finally:
            # Immediately, and in a ``finally``: this is the half that keeps the "no buzz"
            # clause true, so it has to survive the move failing as well as succeeding.
            with contextlib.suppress(Exception):
                await self._servo.relax(axis.channel)
        self._forget_if_current()

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
