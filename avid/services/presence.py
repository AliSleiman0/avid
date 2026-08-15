"""``PresenceService`` — the loop that turns frames into decisions (#223, SDS §3.6.1).

Everything upstream of this is pure or a device. This is what joins them: poll the camera,
run the detector, feed the pure hysteresis filter, publish what it decides. §3.6.1's inventory
row is unusual and worth noticing — this service's "subscribes to" column is **``—`` (polls
camera port)**. It is the only service in the system driven by a clock rather than by the bus,
which makes it structurally closest to ``AudioService``: it owns a real loop task, so the
composition root hands it to the lifecycle to ``start``/``stop``.

**The resource budget is the design constraint, not a nice-to-have.** §2.7.1: *"Vision must
not exceed 1 core; run detection at ≤5 fps, not 30."* §3.8.2 gives capture and inference their
own normative rows and says *the same pool* for both. Read that as an instruction: **one pool,
one thread, both jobs.** Two pools is two cores, and the default ``asyncio.to_thread`` pool is
sized to the CPU count, so using it would quietly put this work on up to eight threads. The
pool is built by the composition root (P3 governs *construction*) and owned here (it is this
service's lifetime that bounds it), and both adapters are handed the same object.

Measured on the Pi at the shipped settings: capture 81.9 ms, detect 47.8 ms, **combined 125.9
ms median / 140.6 ms max against a 200 ms period** — 63%, serialised on one thread by
construction, because there is only one thread to serialise on.

**It drives the two state rows that have existed since M0 with nothing able to reach them**
(#224): ``SLEEPING -> IDLE`` when someone sits down, and ``IDLE -> SLEEPING`` after ten minutes
of sustained absence. Both by **direct awaited call**, never by an event — losing a state
change would be a correctness bug and the bus is explicitly at-most-once (§9.1.4) — and by
necessity rather than preference, since AC-2 forbids this service from subscribing at all.

**Failures degrade, they never accumulate as absence.** A frame whose capture or detection
raises is *skipped*, not fed to the filter as a negative detection. That distinction is not
pedantry: a failure is not evidence that nobody is there, and twenty seconds of a throwing
detector would otherwise emit a false ``presence_lost`` and nap the robot on someone sitting
right in front of it.

Depends only on the ``Camera``, ``FaceDetector``, ``EventBus`` and ``Clock`` Protocols plus the
shared :class:`~avid.core.state_manager.StateManager` (P2); constructed once by the composition
root (P3). It must not import ``ai``, ``motion``, ``memory`` or ``display`` — presence reaches
everything else as a fact on the bus (P5, enforced by ``.importlinter``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import MutableMapping, Sequence
from concurrent.futures import Executor
from uuid import UUID, uuid4

from avid.core.envelope import envelope
from avid.core.event_bus import Subscription
from avid.core.hal import Detection
from avid.core.ports import Camera, Clock, EventBus, FaceDetector
from avid.core.state_manager import StateManager
from avid.core.tasks import spawn
from avid.domain import RobotState, Trigger
from avid.domain.vision import (
    PresenceGained,
    PresenceLost,
    PresenceParams,
    PresenceState,
    VisionFaceDetected,
    VisionPresenceGained,
    VisionPresenceLost,
    step,
)

_log = logging.getLogger(__name__)

_SOURCE = "PresenceService"

# How long a single capture+detect cycle may overrun its slot before the loop says so. The
# measured combined cost is 126 ms against a 200 ms period, so a cycle at the period is already
# anomalous; logging at twice it keeps a slow frame quiet and a stuck one loud.
_OVERRUN_FACTOR = 2.0

# Consecutive failures before the health map flips and the log escalates. One bad frame is a
# glitch; a run of them is a broken camera, and #226 has to be able to tell the two apart from
# a journal dump alone.
_UNHEALTHY_AFTER = 5


class PresenceService:
    """Poll the camera, filter the detections, publish the decisions (SDS §3.6.1).

    Satisfies the :class:`~avid.core.ports.Service` shape. ``subscriptions()`` returns **empty**
    — this service is a poller, not a subscriber — and that is a design statement rather than
    an unwritten method, so it has its own test (the same call ``MemoryService`` made).

    Because it cannot subscribe, it drives the state machine by **direct call**, exactly as
    ``AudioService`` both publishes ``audio.speech_started`` and calls ``transition()``. Losing
    a state change would be a correctness bug and the bus is explicitly at-most-once (§9.1.4).
    """

    name = _SOURCE

    def __init__(
        self,
        *,
        bus: EventBus,
        clock: Clock,
        state: StateManager,
        camera: Camera,
        detector: FaceDetector,
        executor: Executor,
        fps: int,
        params: PresenceParams,
        nap_after_s: float,
        health: MutableMapping[str, bool] | None = None,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._state = state
        self._camera = camera
        self._detector = detector
        self._executor = executor
        self._period_s = 1.0 / fps if fps > 0 else 0.2
        self._params = params
        self._nap_after_s = nap_after_s
        self._health = health
        self._filter = PresenceState()
        self._loop_task: asyncio.Task[None] | None = None
        self._nap_task: asyncio.Task[None] | None = None
        # Public and assertable: how many frames were judged, and how many in a row failed.
        # The second is what makes a sustained fault visible rather than silent (AC-6).
        self.frames = 0
        self.failures = 0
        self._consecutive_failures = 0

    # --- SDS §9.2 service shape ----------------------------------------------------------

    async def start(self) -> None:
        """Start the camera and launch the capture loop.

        A camera that will not start is **logged and marked unhealthy, not fatal** — the loop
        does not launch and nothing is published. A robot with a broken camera should be one
        that never notices anyone, not one that will not boot: vision is a §7.1 "Could", and
        refusing to start would take conversation and memory down with it.
        """
        try:
            await self._camera.start()
        except Exception:
            self._mark_unhealthy()
            _log.exception(
                "%s: camera failed to start — presence detection is disabled for this "
                "run; the robot will never notice anyone, but everything else works",
                _SOURCE,
            )
            return
        self._loop_task = spawn(self._capture_loop(), name="PresenceService.capture")

    async def stop(self) -> None:
        """Unwind within the §9.2 5 s budget. Idempotent.

        The order is load-bearing. Cancel the loop first so nothing new is submitted, then
        stop the camera, then drain the pool — and drain it **from the default pool**, so the
        one in-flight capture+inference (up to ~140 ms) is joined off the event loop and P8's
        50 ms slow-callback gate stays clean. ``shutdown(wait=False)`` would leave a
        non-daemon thread for the interpreter's ``atexit`` to join, which is a shutdown hang
        waiting to happen.
        """
        # The pending nap is cancelled and **abandoned, never fired**: a shutdown is not a nap,
        # and driving IDLE -> SLEEPING mid-teardown would publish state.transitioned into a
        # closing bus. Cancelled first so nothing can fire while the loop unwinds.
        self._cancel_nap()
        task = self._loop_task
        self._loop_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(Exception):
            await self._camera.stop()
        await asyncio.to_thread(self._executor.shutdown, True, cancel_futures=True)

    def subscriptions(self) -> Sequence[Subscription]:
        """**Nothing.** §3.6.1 lists this service's inputs as ``— (polls camera port)``: it is
        the only clock-driven service in the system, and the empty return is the statement of
        that, not an oversight. A test asserts it."""
        return ()

    # --- the capture loop (§2.7.1, §3.8.2) -----------------------------------------------

    async def _capture_loop(self) -> None:
        """One capture + detect + filter per frame period, until cancelled.

        Paced by sleeping the *remainder* of the period rather than a flat interval, so the
        loop holds ≤5 fps instead of drifting to (period + work) — and so a frame that overran
        does not silently become the new cadence, which is exactly the finding #226 AC-2 asks
        to be recorded rather than quietly absorbed.
        """
        while True:
            started_ns = self._clock.monotonic_ns()
            try:
                detections = await self._observe()
            except Exception:
                self._on_failure()
            else:
                self._on_success()
                await self._advance(detections, at_s=started_ns / 1_000_000_000)

            elapsed_s = (self._clock.monotonic_ns() - started_ns) / 1_000_000_000
            if elapsed_s > self._period_s * _OVERRUN_FACTOR:
                _log.warning(
                    "%s: a frame took %.0f ms against a %.0f ms period — the loop cannot "
                    "hold its configured rate (SDS §2.7.1)",
                    _SOURCE,
                    elapsed_s * 1000,
                    self._period_s * 1000,
                )
            await self._clock.sleep(max(0.0, self._period_s - elapsed_s))

    async def _observe(self) -> Sequence[Detection]:
        """One frame, judged. Both halves run on the shared single-thread pool (§3.8.2)."""
        frame = await self._camera.capture()
        return await self._detector.detect(frame)

    async def _advance(self, detections: Sequence[Detection], *, at_s: float) -> None:
        """Fold one frame's detections into the filter and publish anything it decided.

        ``confidence`` is the frame's **best** detection, because the question the filter asks
        is "is anyone here", and the most confident face is the strongest evidence for it. The
        filter compares it against ``[vision] confidence_threshold``; the adapter's own floor
        sits deliberately lower, so the deciding happens here where it is pure and replayable.
        """
        self.frames += 1
        best = max(detections, key=lambda d: d.confidence, default=None)
        self._filter, decision = step(
            self._filter,
            at_s=at_s,
            detected=best is not None,
            confidence=best.confidence if best is not None else 0.0,
            params=self._params,
        )
        if decision is None:
            return

        # A fresh id per DECISION, not per turn. §9.1.1 mints correlation ids only at a turn's
        # origin (audio.speech_started / behavior.trigger_fired) and presence originates no
        # conversation — so this is a *causal-chain* id instead: it threads the decision, the
        # events it publishes and the state transition it drives, so one grep reconstructs
        # "person arrived at 14:02 -> robot woke" from a journal.
        correlation_id = uuid4()
        if isinstance(decision, PresenceGained):
            await self._on_gained(decision, detections, correlation_id)
        else:
            await self._on_lost(decision, correlation_id)

    async def _on_gained(
        self,
        decision: PresenceGained,
        detections: Sequence[Detection],
        correlation_id: UUID,
    ) -> None:
        """Cancel any pending nap, publish the arrival, and wake the robot (#224).

        **Cancel first, publish second.** #226 AC-6 is "leave and come back inside ten minutes
        and confirm the robot did *not* nap", and cancelling before the awaits means that case
        cannot race: the timer is gone before anything else can be scheduled.
        """
        self._cancel_nap()
        await self._bus.publish(
            VisionPresenceGained(
                **envelope(
                    clock=self._clock, correlation_id=correlation_id, source=_SOURCE
                ),
                confidence=decision.confidence,
            )
        )
        # Exactly one face_detected per presence_gained, from the frame that tipped the
        # decision (#219 pinned this). It keeps the §9.1.3 payload verbatim while making
        # published volume equal decision volume — bounded by construction, not by a tuned
        # rate limit. Largest by area, not first: "best-first" and "largest-first" are
        # different orders and the catalog asks for the largest.
        if detections:
            largest = max(detections, key=lambda d: d.box.area)
            await self._bus.publish(
                VisionFaceDetected(
                    **envelope(
                        clock=self._clock, correlation_id=correlation_id, source=_SOURCE
                    ),
                    count=len(detections),
                    largest_bbox=largest.box,
                )
            )
        # SLEEPING -> IDLE. Called **unconditionally**, without checking the current state
        # first: the table is the single source of truth, and a `if state is SLEEPING` guard
        # here would be a second copy of it — exactly the papering-over #224 AC-5 forbids.
        # StateManager already logs-and-ignores the other six states at WARNING (§3.10.3), and
        # the noise is bounded to one line per *decision* because the hysteresis filter already
        # collapsed the edges. Six self-loop rows would be worse: each would publish a spurious
        # state.transitioned to every subscriber.
        await self._state.transition(
            Trigger.VISION_PRESENCE_GAINED, correlation_id=correlation_id
        )

    async def _on_lost(self, decision: PresenceLost, correlation_id: UUID) -> None:
        """Publish the departure and arm the nap (#224).

        The timer is *armed by the edge*, not polled — §3.10.1's nap follows **sustained**
        absence, and the filter has already established that. The same ``correlation_id``
        threads the event and the transition ten minutes later, so one grep shows "person left
        at 14:02 → robot napped at 14:12".
        """
        await self._bus.publish(
            VisionPresenceLost(
                **envelope(
                    clock=self._clock, correlation_id=correlation_id, source=_SOURCE
                ),
                absent_for_s=decision.absent_for_s,
            )
        )
        self._arm_nap(correlation_id=correlation_id)

    # --- the 10-minute nap (§3.10.1, #224) -------------------------------------------------

    def _arm_nap(self, *, correlation_id: UUID) -> None:
        """Start the sustained-absence timer, replacing any pending one.

        Cancel-then-arm is belt and braces: the filter's edge idempotence makes ``lost`` →
        ``lost`` inexpressible without an intervening ``gained`` (which cancels), so there
        should never be a timer to replace. It is one line, and a test asserts at most one nap
        task exists at a time.
        """
        self._cancel_nap()
        self._nap_task = spawn(self._nap(correlation_id), name="PresenceService.nap")

    def _cancel_nap(self) -> None:
        """Drop any pending nap. Synchronous — the capture loop calls it and must not stall."""
        task = self._nap_task
        self._nap_task = None
        if task is not None and not task.done():
            # spawn()'s done-callback ignores CancelledError, so a cancelled nap is a clean
            # teardown rather than a reported death.
            task.cancel()

    async def _nap(self, correlation_id: UUID) -> None:
        """Sleep out the absence, then drive ``IDLE -> SLEEPING`` — retrying if it was ignored.

        ⚠️ **The retry is why this is a loop, and it is not the poll AC-2 forbids.** The nap
        can come due in a state that has no row: someone leaves at 14:02, a noise opens a
        session at 14:05, and at 14:12 the trigger arrives in SPEAKING, where it is illegal.
        ``StateManager`` logs and ignores it — correctly — and without this loop **nothing
        would ever re-arm it**, leaving the robot awake indefinitely in an empty room. That is
        the `THINK_TIMEOUT` shape that cost the bench 54 seconds (AVID-171), and #224 exists
        partly to not repeat it.

        Re-arming waits the *full* interval again rather than retrying immediately, because a
        conversation is itself evidence that someone was there — napping three seconds after
        the robot stops speaking would be worse than not napping at all. Presence stays
        edge-driven throughout: nothing here asks "is anyone here", it only retries **one
        transition** that a busy state refused.

        Sleeps on the injected :class:`~avid.core.ports.Clock`, so the ten minutes are a
        millisecond in a test.
        """
        while True:
            await self._clock.sleep(self._nap_after_s)
            reached = await self._state.transition(
                Trigger.PRESENCE_LOST_TIMEOUT, correlation_id=correlation_id
            )
            if reached is RobotState.SLEEPING:
                return

    # --- failure handling (AC-6) ----------------------------------------------------------

    def _on_failure(self) -> None:
        """A frame that could not be captured or judged. **Skipped, never counted as absence.**

        The loop continues — a camera fault or a corrupt frame must not kill the process or
        the conversation — but the frame does not reach the filter, because a failure is not
        evidence that the room is empty. Feeding it in as a negative would let a broken
        detector put the robot to sleep on someone sitting right there.
        """
        self.failures += 1
        self._consecutive_failures += 1
        if self._consecutive_failures == _UNHEALTHY_AFTER:
            self._mark_unhealthy()
            _log.exception(
                "%s: %d consecutive frames failed — presence is now unreliable and the "
                "health map says so (SDS §3.12.3)",
                _SOURCE,
                self._consecutive_failures,
            )
        else:
            _log.warning(
                "%s: frame %d failed and was skipped, not counted as absence",
                _SOURCE,
                self.frames + self.failures,
                exc_info=True,
            )

    def _on_success(self) -> None:
        if self._consecutive_failures:
            _log.info(
                "%s: recovered after %d failed frame(s)",
                _SOURCE,
                self._consecutive_failures,
            )
            self._consecutive_failures = 0
            if self._health is not None:
                self._health["face_detector"] = True

    def _mark_unhealthy(self) -> None:
        if self._health is not None:
            self._health["face_detector"] = False


__all__ = ["PresenceService"]
