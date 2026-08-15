"""``PresenceService`` — the capture loop, the pool, and the decisions (#223, SDS §3.6.1).

The gate criterion in miniature lives here: an hour of frames at a realistic miss rate must
produce single-digit events, and it runs in CI on a laptop with no camera. The rest of the file
covers the three things that are this service's rather than the filter's or the adapters' — that
it subscribes to nothing, that it owns exactly one thread, and that a failing frame is *skipped*
rather than counted as absence.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Iterator
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

import pytest

from avid.adapters.camera import FakeCamera
from avid.adapters.clock import FakeClock
from avid.adapters.face_detector import FakeFaceDetector
from avid.core.event_bus import AsyncioEventBus
from avid.core.hal import BBox, Detection, Frame
from avid.core.state_manager import StateManager
from avid.domain import Event, RobotState
from avid.domain.vision import (
    PresenceParams,
    VisionFaceDetected,
    VisionPresenceGained,
    VisionPresenceLost,
)
from avid.services.presence import PresenceService

_FPS = 5
_PERIOD = 1.0 / _FPS
# Small on purpose: 18,000 frames at 640x480 would allocate 16 GB of synthetic bytes to carry
# one bit of information. The service never looks at pixels — the detector does.
_W, _H = 32, 24
_PARAMS = PresenceParams(confidence_threshold=0.6, gain_window_s=0.6, lose_window_s=4.0)


class _Recorder:
    """Collects every event published, in order. A list is enough — the bus is real."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)


class _ScriptedDetector:
    """A detector driven by an explicit ``(detected, confidence)`` timeline.

    ``FakeFaceDetector`` keys off the frame's bytes, which is right for proving the two fakes
    compose (the contract suite does that). Here the *timeline* is the input under test, so it
    is stated directly rather than smuggled through a camera flag.
    """

    def __init__(self, script: list[tuple[bool, float]], *, faces: int = 1) -> None:
        self._script = script
        self._faces = faces
        self.calls = 0

    async def detect(self, frame: Frame) -> tuple[Detection, ...]:
        index = min(self.calls, len(self._script) - 1)
        self.calls += 1
        detected, confidence = self._script[index]
        if not detected:
            return ()
        return tuple(
            Detection(
                confidence=confidence,
                box=BBox(x=0, y=0, w=10 - i, h=10 - i),
            )
            for i in range(self._faces)
        )


def _service(
    detector: Any,
    *,
    bus: AsyncioEventBus,
    clock: FakeClock,
    camera: Any = None,
    executor: Executor | None = None,
    health: dict[str, bool] | None = None,
    params: PresenceParams = _PARAMS,
) -> PresenceService:
    return PresenceService(
        bus=bus,
        clock=clock,
        state=StateManager(bus=bus, clock=clock, initial=RobotState.IDLE),
        camera=camera
        if camera is not None
        else FakeCamera(width=_W, height=_H, fps=_FPS),
        detector=detector,
        executor=executor
        if executor is not None
        else ThreadPoolExecutor(max_workers=1),
        fps=_FPS,
        params=params,
        health=health,
    )


async def _drive(service: PresenceService, clock: FakeClock, frames: int) -> None:
    """Run the loop for *frames* periods of virtual time, then unwind it and let the bus drain.

    The bus dispatches to subscriber workers concurrently and its ``stop`` *cancels* them, so a
    test that asserts on delivered events has to let those workers run first. Yielding the loop
    a bounded number of times is enough — every handler here is a list append — and it is
    bounded rather than a sleep so a hang shows up as a failed assertion, not a slow suite.
    """
    await service.start()
    for _ in range(frames):
        await clock.advance(_PERIOD)
    await service.stop()
    await _settle()


async def _settle(turns: int = 50) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


# --- AC-2: it subscribes to nothing, and that is a statement ------------------


def test_subscriptions_is_empty_because_this_service_is_a_poller() -> None:
    """§3.6.1 lists this service's inputs as ``— (polls camera port)``. It is the only
    clock-driven service in the system, so the empty return is a design statement rather than
    an unwritten method — which is why it gets an assertion of its own (the same call
    ``MemoryService`` made). It is also why the state machine is driven by a **direct call**:
    a service that cannot subscribe cannot react to its own published fact."""
    bus = AsyncioEventBus(clock=FakeClock())
    service = _service(_ScriptedDetector([(False, 0.0)]), bus=bus, clock=FakeClock())
    assert service.subscriptions() == ()


# --- AC-4: one pool, one thread, and this service closes it ------------------


async def test_it_shuts_down_the_one_thread_pool_it_was_given() -> None:
    """§3.8.2 gives capture and inference the *same dedicated single-thread pool*, and the
    budget is stated in cores: two pools is two cores, and ``asyncio.to_thread``'s default pool
    is sized to the CPU count. The composition root builds it (P3 governs construction); this
    service's lifetime bounds it, so its ``stop`` is what drains it."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vision")
    assert pool._max_workers == 1

    service = _service(
        _ScriptedDetector([(False, 0.0)]), bus=bus, clock=clock, executor=pool
    )
    async with bus:
        await _drive(service, clock, frames=2)

    # Submitting to a shut-down pool raises — the assertable proof that stop() drained it
    # rather than leaving a non-daemon thread for the interpreter's atexit to join.
    with pytest.raises(RuntimeError):
        pool.submit(lambda: None)


async def test_stop_is_idempotent_and_survives_never_having_started() -> None:
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    service = _service(_ScriptedDetector([(False, 0.0)]), bus=bus, clock=clock)
    await service.stop()
    await service.stop()


# --- AC-5: decisions only, and the gate criterion in miniature ---------------


async def test_an_hour_of_frames_with_a_realistic_miss_rate_publishes_two_events() -> (
    None
):
    """**The gate criterion in miniature, and it runs in CI.**

    18,000 frames — an hour at 5 fps — of one person sitting at a desk, with a seeded 5%
    per-frame miss rate. A detector that misses one frame in twenty is a *good* detector, and
    unfiltered this input contains roughly 900 falling edges: §9.1.3's *"hundreds of events an
    hour"*, every one of which is a ``presence_lost`` that could nap the robot on someone still
    sitting there.

    The correct answer is stated precisely rather than as "single digit", because a bound
    nobody pinned is a bound that gets loosened: **exactly one ``presence_gained``, zero
    ``presence_lost``, and one ``face_detected``** — two events. The count of
    ``face_detected`` is bounded by construction (one per gain), not by tuning, so this
    assertion cannot drift when #225 re-tunes the windows.
    """
    rng = random.Random(8)
    script = [(rng.random() > 0.05, 0.9) for _ in range(18_000)]
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    recorder = _Recorder()
    for event_type in (VisionPresenceGained, VisionPresenceLost, VisionFaceDetected):
        bus.subscribe(event_type, recorder.handle, name=f"test.{event_type.__name__}")

    service = _service(_ScriptedDetector(script), bus=bus, clock=clock)
    async with bus:
        await _drive(service, clock, frames=len(script))

    kinds = [type(e).__name__ for e in recorder.events]
    assert kinds.count("VisionPresenceGained") == 1
    assert kinds.count("VisionPresenceLost") == 0
    assert kinds.count("VisionFaceDetected") == 1
    assert len(recorder.events) == 2


async def test_a_clean_arrival_and_departure_publish_the_catalogued_payloads() -> None:
    """The events carry what §9.1.3 says they carry, computed by the filter rather than by the
    service: a confidence for the gain, and an ``absent_for_s`` measured from the last positive
    detection for the loss (#219 pinned that meaning; #224's nap hangs off it)."""
    script = [(True, 0.9)] * 20 + [(False, 0.0)] * 40
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    recorder = _Recorder()
    for event_type in (VisionPresenceGained, VisionPresenceLost, VisionFaceDetected):
        bus.subscribe(event_type, recorder.handle, name=f"test.{event_type.__name__}")

    service = _service(_ScriptedDetector(script, faces=3), bus=bus, clock=clock)
    async with bus:
        await _drive(service, clock, frames=len(script))

    gained = [e for e in recorder.events if isinstance(e, VisionPresenceGained)]
    lost = [e for e in recorder.events if isinstance(e, VisionPresenceLost)]
    faces = [e for e in recorder.events if isinstance(e, VisionFaceDetected)]
    assert len(gained) == 1 and len(lost) == 1 and len(faces) == 1
    assert gained[0].confidence == pytest.approx(0.9)
    assert lost[0].absent_for_s > _PARAMS.lose_window_s
    # The largest box by area, not the first — "best-first" and "largest-first" are different
    # orders, and the catalog asks for the largest.
    assert faces[0].count == 3
    assert faces[0].largest_bbox.area == max(
        BBox(x=0, y=0, w=10 - i, h=10 - i).area for i in range(3)
    )


async def test_face_detected_shares_the_correlation_id_of_the_gain_it_evidences() -> (
    None
):
    """One causal-chain id per decision (§3.12.2). Presence originates no turn — §9.1.1 mints
    turn ids only at ``audio.speech_started`` / ``behavior.trigger_fired`` — so this id exists
    to make ``grep <id>`` reconstruct "someone arrived, here is the frame, the robot woke"."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    recorder = _Recorder()
    for event_type in (VisionPresenceGained, VisionFaceDetected):
        bus.subscribe(event_type, recorder.handle, name=f"test.{event_type.__name__}")

    service = _service(_ScriptedDetector([(True, 0.9)] * 20), bus=bus, clock=clock)
    async with bus:
        await _drive(service, clock, frames=20)

    assert len({e.correlation_id for e in recorder.events}) == 1
    assert all(e.source == "PresenceService" for e in recorder.events)


# --- AC-6: a failure degrades, and is never counted as absence ---------------


class _FlakyDetector:
    """Raises for a scripted window, then recovers. Detection is otherwise positive."""

    def __init__(self, *, fail_from: int, fail_until: int) -> None:
        self._fail_from = fail_from
        self._fail_until = fail_until
        self.calls = 0

    async def detect(self, frame: Frame) -> tuple[Detection, ...]:
        index = self.calls
        self.calls += 1
        if self._fail_from <= index < self._fail_until:
            raise RuntimeError("the detector fell over")
        return (Detection(confidence=0.9, box=BBox(x=0, y=0, w=8, h=8)),)


async def test_a_throwing_detector_is_skipped_not_counted_as_absence() -> None:
    """**The failure this service exists to not have.** A raising detector is caught, logged and
    the loop continues — but the frame is *skipped*, never fed to the filter as a negative.

    A failure is not evidence that the room is empty. Counting it as one means a broken
    detector emits ``presence_lost`` and naps the robot on someone sitting right in front of
    it: the exact silent-failure shape the milestone is written around, arriving through the
    back door. The window here is 30 frames — six seconds, longer than the exit window — so a
    filter fed the failures would certainly have decided absence.
    """
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    recorder = _Recorder()
    bus.subscribe(VisionPresenceLost, recorder.handle, name="test.lost")

    detector = _FlakyDetector(fail_from=10, fail_until=40)
    service = _service(detector, bus=bus, clock=clock)
    async with bus:
        await _drive(service, clock, frames=60)

    assert recorder.events == []
    assert service.failures == 30
    assert service.frames == 30  # only the frames that were actually judged


async def test_sustained_failure_is_visible_in_the_health_map_and_recovers() -> None:
    """Loud drops are a tuning signal; silent ones are a debugging catastrophe. A run of
    failures flips the health map so a bench operator sees it in one place, and a recovery
    flips it back — otherwise the first glitch of a long run would leave the robot permanently
    marked broken."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    health = {"face_detector": True}
    detector = _FlakyDetector(fail_from=0, fail_until=8)
    service = _service(detector, bus=bus, clock=clock, health=health)

    async with bus:
        await service.start()
        for _ in range(6):
            await clock.advance(_PERIOD)
        assert health["face_detector"] is False
        for _ in range(6):
            await clock.advance(_PERIOD)
        await service.stop()

    assert health["face_detector"] is True


async def test_a_camera_that_will_not_start_disables_vision_without_killing_the_boot() -> (
    None
):
    """A robot with a broken camera should be a robot that never notices anyone, not a robot
    that will not boot. Vision is a §7.1 "Could"; refusing to start would take conversation and
    memory down with it."""

    class _DeadCamera:
        async def start(self) -> None:
            raise OSError("no camera on this rig")

        async def stop(self) -> None: ...

        async def capture(self) -> Frame:  # pragma: no cover - never reached
            raise AssertionError("capture must not be called after a failed start")

        @property
        def capabilities(self) -> Any:  # pragma: no cover - never read here
            raise AssertionError

    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    health = {"face_detector": True}
    service = _service(
        _ScriptedDetector([(True, 0.9)]),
        bus=bus,
        clock=clock,
        camera=_DeadCamera(),
        health=health,
    )
    async with bus:
        await service.start()  # must not raise
        await clock.advance(10.0)
        await service.stop()

    assert health["face_detector"] is False
    assert service.frames == 0


# --- the loop's own promises -------------------------------------------------


async def test_the_loop_paces_itself_at_the_configured_rate() -> None:
    """≤5 fps is a budget, not an aspiration (§2.7.1). The loop sleeps the *remainder* of the
    period rather than a flat interval, so work does not drift the cadence to (period + work) —
    and so a frame that overran cannot silently become the new rate, which is the finding #226
    AC-2 asks to be recorded rather than absorbed."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    detector = _ScriptedDetector([(False, 0.0)])
    service = _service(detector, bus=bus, clock=clock)

    async with bus:
        await _drive(service, clock, frames=10)

    assert detector.calls == 10


async def test_it_is_the_only_caller_of_camera_capture() -> None:
    """Mirrors ``ExpressionService``'s "only caller of ``render()``" test. The camera is a
    shared, stateful device; a second caller would interleave captures with this loop's and
    make the frame timeline — which #225 commits and replays — unreproducible."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    camera = FakeCamera(width=_W, height=_H, fps=_FPS)
    service = _service(FakeFaceDetector(), bus=bus, clock=clock, camera=camera)
    async with bus:
        await _drive(service, clock, frames=7)

    assert camera.captures == 7


async def test_a_frame_that_overruns_its_period_says_so_rather_than_absorbing_it() -> (
    None
):
    """§2.7.1 caps the loop at ≤5 fps and #226 AC-2 asks for a rate that cannot be held to be
    *recorded as a finding, not quietly lowered*. A loop that simply slept the remainder would
    drift to (period + work) in silence; this one notices and says which numbers it saw.

    Measured combined cost on the Pi is 126 ms against a 200 ms period, so a cycle *at* the
    period is already anomalous — the warning fires at twice it, which keeps a merely slow
    frame quiet and a stuck one loud.
    """

    class _SlowDetector:
        """Burns virtual time inside the frame, the way a real overrun would."""

        def __init__(self, clock: FakeClock) -> None:
            self._clock = clock
            self.calls = 0

        async def detect(self, frame: Frame) -> tuple[Detection, ...]:
            self.calls += 1
            if self.calls == 1:
                await self._clock.advance(_PERIOD * 3)
            return ()

    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    service = _service(_SlowDetector(clock), bus=bus, clock=clock)

    async with bus:
        with _presence_warnings() as records:
            await _drive(service, clock, frames=3)

    assert any("cannot hold its configured rate" in message for message in records)


@contextmanager
def _presence_warnings() -> Iterator[list[str]]:
    """Capture ``avid.services.presence``'s WARNING lines as plain strings."""
    records: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("avid.services.presence")
    handler = _Handler(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
