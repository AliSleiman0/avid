"""The drawer: affect in, frame on glass (AVID-72, SDS §3.6.1).

Real :class:`AsyncioEventBus`, real :class:`FakeDisplay`, real :class:`FakeClock`, no mocks —
``unittest.mock`` is banned outside ``tests/adapters/`` (SDS §14.3) and would be actively worse
here: the properties under test are *about* dispatch, port calls and envelope arithmetic, which
a mock would assert away rather than exercise.

Two local ``FakeDisplay`` subclasses appear below. They are instrumentation, not second fakes:
both still encode and write real PNGs through the real adapter, and only add a signal a test
can await or a failure a test can provoke.

Waiting is always on an :class:`asyncio.Event`, never a sleep. The bus **cancels** its workers
on ``stop()`` rather than draining them, so "publish then sleep a bit" is a race that passes
locally and fails on a loaded CI box.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import AsyncIterator
from pathlib import Path
from typing import NamedTuple
from uuid import UUID, uuid4

import pytest

from avid.adapters.clock import FakeClock
from avid.adapters.display import FakeDisplay
from avid.core.affect_map import baseline_affect
from avid.core.event_bus import AsyncioEventBus
from avid.core.hal import DisplayFrame
from avid.domain import (
    Affect,
    AffectChanged,
    Event,
    RobotState,
    StateTransitioned,
    SystemHandlerFailed,
    Trigger,
)
from avid.domain.events import REASON_HANDLER_RAISED
from avid.services.expression import ExpressionService

# A publish reaches its subscriber in a couple of scheduler turns; a whole second is a generous
# ceiling that still fails fast if the bus ever wedges.
_ARRIVAL_TIMEOUT_S = 1.0

# The real panel (AVID-55), not FakeDisplay's 240x240 default — so the "sized from the display"
# assertion is testing something rather than agreeing with a coincidence.
_PANEL = (480, 320)

# Rooted at the repo, not the cwd, so the contact sheet lands in the same place however pytest
# was invoked: tests/services/test_expression.py -> tests/services -> tests -> repo.
REPO_ROOT = Path(__file__).resolve().parents[2]


class _RecordingDisplay(FakeDisplay):
    """:class:`FakeDisplay` plus a signal, so a test can await the *n*-th render.

    Renders are silent by design — this service publishes nothing — so the frame count is the
    only edge a test can wait on.
    """

    def __init__(self, *, out_dir: Path, resolution: tuple[int, int] = _PANEL) -> None:
        super().__init__(out_dir=out_dir, resolution=resolution)
        self._arrived = asyncio.Event()

    async def render(self, frame: DisplayFrame) -> None:
        await super().render(frame)
        self._arrived.set()

    async def wait_for_frames(self, count: int) -> None:
        """Block until at least *count* frames have been rendered, or fail the test."""
        async with asyncio.timeout(_ARRIVAL_TIMEOUT_S):
            while self.frames_rendered < count:
                self._arrived.clear()
                if self.frames_rendered >= count:
                    return
                await self._arrived.wait()

    async def settle(self) -> None:
        """Let the bus drain whatever is queued, for the "nothing happened" assertions."""
        for _ in range(10):
            await asyncio.sleep(0)


class _FlakyDisplay(_RecordingDisplay):
    """A display that fails its first render and then behaves — the AC-5 rig.

    A panel that throws once (a transient ``ioctl``, a busy SPI bus) is the realistic failure,
    and the point of the test is that the *next* frame still lands.
    """

    def __init__(self, *, out_dir: Path, resolution: tuple[int, int] = _PANEL) -> None:
        super().__init__(out_dir=out_dir, resolution=resolution)
        self.failed = False

    async def render(self, frame: DisplayFrame) -> None:
        if not self.failed:
            self.failed = True
            raise OSError("panel went away")
        await super().render(frame)


class _Collector:
    """Records every event of the types it is registered for.

    Used only for the negative assertion that ``ExpressionService`` publishes nothing, and for
    reading the bus's own ``system.handler_failed``.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._arrived = asyncio.Event()

    async def handle(self, event: Event) -> None:
        self.events.append(event)
        self._arrived.set()

    async def wait_for(self, count: int) -> None:
        async with asyncio.timeout(_ARRIVAL_TIMEOUT_S):
            while len(self.events) < count:
                self._arrived.clear()
                if len(self.events) >= count:
                    return
                await self._arrived.wait()


class Rig(NamedTuple):
    """Everything a test needs, wired the way the composition root will wire it in #73."""

    service: ExpressionService
    bus: AsyncioEventBus
    display: _RecordingDisplay
    clock: FakeClock
    collector: _Collector


def _register(bus: AsyncioEventBus, service: ExpressionService) -> None:
    """Register what the service *declared*. This is the composition root's job (SDS §9.2);
    the test does it here because #73 has not written it yet."""
    for sub in service.subscriptions():
        bus.subscribe(
            sub.event_type,
            sub.handler,
            name=sub.name,
            policy=sub.policy,
            maxsize=sub.maxsize,
        )


def _build(
    bus: AsyncioEventBus, display: _RecordingDisplay, clock: FakeClock
) -> ExpressionService:
    return ExpressionService(bus=bus, display=display, clock=clock)


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    """A started bus with the service's declared subscriptions registered.

    ``tmp_path`` so no test litters ``.artifacts/``. All ``subscribe()`` calls precede
    ``bus.start()`` — the bus freezes its subscriber graph there.
    """
    clock = FakeClock()
    bus = AsyncioEventBus()
    display = _RecordingDisplay(out_dir=tmp_path)
    service = _build(bus, display, clock)
    collector = _Collector()

    _register(bus, service)
    # Everything this service could conceivably emit, so "it published nothing" is a claim
    # about the bus rather than about the module's source text.
    bus.subscribe(AffectChanged, collector.handle, name="test.affect")
    bus.subscribe(SystemHandlerFailed, collector.handle, name="test.failed")

    await bus.start()
    await service.start()
    try:
        yield Rig(service, bus, display, clock, collector)
    finally:
        await service.stop()
        await bus.stop()


def _transitioned(
    *,
    to: RobotState,
    from_: RobotState = RobotState.IDLE,
    correlation_id: UUID | None = None,
    clock: FakeClock | None = None,
) -> StateTransitioned:
    """A ``state.transitioned`` as ``StateManager`` would publish it."""
    source = clock or FakeClock()
    return StateTransitioned(
        event_id=uuid4(),
        correlation_id=correlation_id or uuid4(),
        timestamp_ms=source.now() * 1000,
        monotonic_ns=source.monotonic_ns(),
        source="StateManager",
        from_=from_,
        to=to,
        trigger=Trigger.SYSTEM_STARTED,
    )


def _affect_changed(
    *,
    affect: Affect,
    previous: Affect = Affect.IDLE,
    correlation_id: UUID | None = None,
    clock: FakeClock | None = None,
) -> AffectChanged:
    """An ``affect.changed`` as ``AffectService`` would publish it."""
    source = clock or FakeClock()
    return AffectChanged(
        event_id=uuid4(),
        correlation_id=correlation_id or uuid4(),
        timestamp_ms=source.now() * 1000,
        monotonic_ns=source.monotonic_ns(),
        source="AffectService",
        affect=affect,
        tier=2,
        previous=previous,
    )


# --- AC-1: precomputed at construction, sized from the display -----------------------------


def test_every_affect_is_cached_at_construction(tmp_path: Path) -> None:
    """All eight, composed before a single event arrives. The hot path is a dict lookup.

    Iterating ``Affect`` rather than listing members is what makes a ninth affect cached the
    day it is added rather than a ``KeyError`` on the Pi.
    """
    display = _RecordingDisplay(out_dir=tmp_path)
    service = _build(AsyncioEventBus(), display, FakeClock())

    assert len(Affect) == 8
    for affect in Affect:
        assert isinstance(service.face(affect), DisplayFrame)


def test_faces_are_sized_from_the_injected_display(tmp_path: Path) -> None:
    """Sized from ``display.resolution``, never a constant. The panel is 480x320 and
    ``FakeDisplay``'s bare default is 240x240 — a hardcoded size would pass one and fail the
    other, which is exactly the bug this asserts away."""
    display = _RecordingDisplay(out_dir=tmp_path, resolution=(320, 200))
    service = _build(AsyncioEventBus(), display, FakeClock())

    frame = service.face(Affect.HAPPY)
    assert (frame.width, frame.height) == (320, 200)
    assert frame.format == "RGB888"
    assert len(frame.pixels) == 320 * 200 * 3


def test_construction_renders_nothing(tmp_path: Path) -> None:
    """Precomputing is composing frames, not pushing them. Nothing reaches the panel until an
    event says the affect changed."""
    display = _RecordingDisplay(out_dir=tmp_path)
    _build(AsyncioEventBus(), display, FakeClock())

    assert display.frames_rendered == 0


def test_subscriptions_are_declared_not_registered(tmp_path: Path) -> None:
    """The service says what it wants; the composition root decides (SDS §9.2)."""
    bus = AsyncioEventBus()
    service = _build(bus, _RecordingDisplay(out_dir=tmp_path), FakeClock())

    assert bus._subs == {}
    names = {sub.name for sub in service.subscriptions()}
    assert names == {
        "ExpressionService.affect_changed",
        "ExpressionService.state_transitioned",
    }


# --- AC-2: both inputs render; nothing is published ----------------------------------------


async def test_affect_changed_renders_the_mapped_face(rig: Rig) -> None:
    service, bus, display, _, _ = rig
    await bus.publish(_affect_changed(affect=Affect.HAPPY))
    await display.wait_for_frames(1)

    assert display.rendered[0] is service.face(Affect.HAPPY)


async def test_state_transitioned_renders_the_tier_1_face_directly(rig: Rig) -> None:
    """The independence that matters: no ``AffectService`` exists in this rig at all, and the
    face is still correct. A dropped ``affect.changed`` — the bus is at-most-once — must not
    leave the robot wearing the wrong expression."""
    service, bus, display, _, _ = rig
    await bus.publish(_transitioned(to=RobotState.LISTENING))
    await display.wait_for_frames(1)

    assert display.rendered[0] is service.face(Affect.LISTENING)


@pytest.mark.parametrize("state", list(RobotState))
async def test_every_robot_state_renders_a_face(rig: Rig, state: RobotState) -> None:
    """Exhaustive over the enum: no operational state can reach the panel as a ``KeyError``."""
    service, bus, display, _, _ = rig
    await bus.publish(_transitioned(to=state))
    await display.wait_for_frames(1)

    assert display.rendered[0] is service.face(baseline_affect(state))


async def test_the_service_publishes_nothing(rig: Rig) -> None:
    """Rendering is an effect, not a fact (P4). Asserted against a real bus rather than by
    reading the source, so it stays true however the module is refactored."""
    _, bus, display, _, collector = rig
    await bus.publish(_transitioned(to=RobotState.THINKING))
    await bus.publish(_affect_changed(affect=Affect.CONFUSED))
    await display.wait_for_frames(2)
    await display.settle()

    assert [e for e in collector.events if e.source == "ExpressionService"] == []


async def test_both_inputs_render_independently(rig: Rig) -> None:
    """A state change followed by its ``affect.changed`` renders twice, deliberately.

    There is no "same face as last time" suppression, and this test is why: with one, a service
    that ignored ``affect.changed`` entirely would be indistinguishable from a correct one.
    """
    service, bus, display, _, _ = rig
    await bus.publish(_transitioned(to=RobotState.SPEAKING))
    await display.wait_for_frames(1)
    await bus.publish(_affect_changed(affect=Affect.SPEAKING, previous=Affect.IDLE))
    await display.wait_for_frames(2)

    assert display.frames_rendered == 2
    assert display.rendered[0] is display.rendered[1] is service.face(Affect.SPEAKING)


# --- AC-3 / AC-4: latency, measured honestly -----------------------------------------------


async def test_latency_is_measured_across_the_render(rig: Rig) -> None:
    """Sampled *after* ``render()`` returns, so the threaded framebuffer write is inside the
    number. The clock is advanced between minting the event and publishing it, which is the
    fake-time equivalent of a slow path."""
    service, bus, display, clock, _ = rig
    event = _affect_changed(affect=Affect.SAD, clock=clock)
    await clock.advance(0.005)

    await bus.publish(event)
    await display.wait_for_frames(1)

    assert service.last_latency_ms == pytest.approx(5.0)
    assert service.max_latency_ms == pytest.approx(5.0)


async def test_max_latency_is_a_running_maximum(rig: Rig) -> None:
    """``max`` must survive a subsequent faster render — otherwise it is just ``last`` with a
    longer name, and the worst case, which is the one that breaches O4, is invisible."""
    service, bus, display, clock, _ = rig
    slow = _affect_changed(affect=Affect.SAD, clock=clock)
    await clock.advance(0.008)
    await bus.publish(slow)
    await display.wait_for_frames(1)

    fast = _affect_changed(affect=Affect.HAPPY, clock=clock)
    await clock.advance(0.002)
    await bus.publish(fast)
    await display.wait_for_frames(2)

    assert service.last_latency_ms == pytest.approx(2.0)
    assert service.max_latency_ms == pytest.approx(8.0)


async def test_latency_starts_unmeasured(rig: Rig) -> None:
    service, *_ = rig
    assert service.last_latency_ms is None
    assert service.max_latency_ms == 0.0


async def test_a_wall_clock_step_does_not_touch_the_metric(rig: Rig) -> None:
    """AC-4, and the reason SDS §9.1.1 carries two time fields.

    NTP corrects the Pi's clock seconds after boot — precisely when the first faces render — so
    a metric built on ``timestamp_ms`` would report an hour of latency, or a negative one. Here
    the envelope's wall clock is stepped back an hour while its monotonic reading stays honest;
    an implementation using either ``event.timestamp_ms`` or ``clock.now()`` produces something
    near 3,600,000 and fails loudly. Wall for humans, monotonic for arithmetic.
    """
    service, bus, display, clock, _ = rig
    honest = _affect_changed(affect=Affect.CONFUSED, clock=clock)
    stepped = dataclasses.replace(honest, timestamp_ms=honest.timestamp_ms - 3_600_000)
    await clock.advance(0.005)

    await bus.publish(stepped)
    await display.wait_for_frames(1)

    assert service.last_latency_ms == pytest.approx(5.0)


# --- ordering: two queues race, and the older frame loses ----------------------------------


async def test_a_superseded_event_does_not_overwrite_a_newer_frame(rig: Rig) -> None:
    """The hazard of reading two subscriptions (SDS §3.5): the bus is FIFO *per subscriber*,
    not across them, so an ``affect.changed`` minted before a ``state.transitioned`` can be
    handled after it.

    Rendering last-writer-wins would leave the robot wearing the older affect until the next
    event — which may be a whole conversation away, because nothing here republishes. The
    stale frame is dropped instead, and counted.
    """
    service, bus, display, clock, _ = rig
    stale = _affect_changed(affect=Affect.HAPPY, clock=clock)
    await clock.advance(0.010)
    fresh = _transitioned(to=RobotState.THINKING, clock=clock)

    # Newest first, exactly as the race would deliver them.
    await bus.publish(fresh)
    await display.wait_for_frames(1)
    await bus.publish(stale)
    await display.settle()

    assert display.frames_rendered == 1
    assert display.rendered[0] is service.face(Affect.THINKING)
    assert service.stale_skipped == 1


async def test_an_equally_stamped_event_still_renders(rig: Rig) -> None:
    """Strictly older, not merely not-newer. Two events can share a monotonic reading — the
    clock has finite resolution — and dropping the second would silently lose real frames."""
    service, bus, display, clock, _ = rig
    first = _transitioned(to=RobotState.SPEAKING, clock=clock)
    second = _affect_changed(affect=Affect.HAPPY, clock=clock)

    await bus.publish(first)
    await display.wait_for_frames(1)
    await bus.publish(second)
    await display.wait_for_frames(2)

    assert service.stale_skipped == 0
    assert display.rendered[1] is service.face(Affect.HAPPY)


async def test_a_failed_render_does_not_suppress_the_retry(tmp_path: Path) -> None:
    """The guard advances only on a frame that actually landed. A frame that raised was never
    shown, so letting it mark the timeline would drop the very frame meant to replace it."""
    clock = FakeClock()
    bus = AsyncioEventBus()
    display = _FlakyDisplay(out_dir=tmp_path)
    service = _build(bus, display, clock)
    collector = _Collector()

    _register(bus, service)
    bus.subscribe(SystemHandlerFailed, collector.handle, name="test.failed")
    await bus.start()
    try:
        doomed = _affect_changed(affect=Affect.HAPPY, clock=clock)
        await bus.publish(doomed)
        await collector.wait_for(1)

        # Same stamp as the frame that failed: it must not have been recorded as rendered.
        retry = _affect_changed(affect=Affect.HAPPY, clock=clock)
        await bus.publish(retry)
        await display.wait_for_frames(1)

        assert service.stale_skipped == 0
        assert display.rendered[0] is service.face(Affect.HAPPY)
    finally:
        await bus.stop()


# --- AC-5: a failing panel does not kill the subscriber ------------------------------------


async def test_a_raising_display_is_swallowed_and_the_next_frame_lands(
    tmp_path: Path,
) -> None:
    """The single most important reliability property in the system (SDS §3.5.2): a crashing
    display renderer must not kill the conversation. The bus swallows, republishes
    ``system.handler_failed``, and the worker keeps its subscription."""
    clock = FakeClock()
    bus = AsyncioEventBus()
    display = _FlakyDisplay(out_dir=tmp_path)
    service = _build(bus, display, clock)
    collector = _Collector()

    _register(bus, service)
    bus.subscribe(SystemHandlerFailed, collector.handle, name="test.failed")
    await bus.start()
    try:
        await bus.publish(_affect_changed(affect=Affect.HAPPY))
        await collector.wait_for(1)

        (failure,) = collector.events
        assert isinstance(failure, SystemHandlerFailed)
        assert failure.reason == REASON_HANDLER_RAISED
        assert failure.handler == "ExpressionService.affect_changed"
        assert display.frames_rendered == 0

        # The point of the test: the service is still subscribed and still works.
        await bus.publish(_affect_changed(affect=Affect.SAD))
        await display.wait_for_frames(1)
        assert display.rendered[0] is service.face(Affect.SAD)
    finally:
        await bus.stop()


async def test_a_failed_render_records_no_latency(tmp_path: Path) -> None:
    """A frame that never landed took no time to land. Recording a latency for it would put a
    fictional sample in the metric O4 is graded on."""
    clock = FakeClock()
    bus = AsyncioEventBus()
    display = _FlakyDisplay(out_dir=tmp_path)
    service = _build(bus, display, clock)
    collector = _Collector()

    _register(bus, service)
    bus.subscribe(SystemHandlerFailed, collector.handle, name="test.failed")
    await bus.start()
    try:
        await bus.publish(_affect_changed(affect=Affect.HAPPY))
        await collector.wait_for(1)
        assert service.last_latency_ms is None
    finally:
        await bus.stop()


# --- AC-6: identity and counts, never pixels -----------------------------------------------


async def test_frames_rendered_counts_and_identity_matches_the_cache(rig: Rig) -> None:
    """Identity, not appearance (SDS §14.8): the assertion is that the *cached* frame reached
    the port, which is what proves the precompute is actually on the hot path. What the face
    looks like is a human's job, in the CI artifact."""
    service, bus, display, _, _ = rig
    expected = (Affect.HAPPY, Affect.SAD, Affect.THINKING)
    for affect in expected:
        await bus.publish(_affect_changed(affect=affect))
    await display.wait_for_frames(3)

    assert display.frames_rendered == 3
    # ``is``, not ``==``: DisplayFrame is a frozen dataclass, so equality would compare 460 KB
    # of pixels three times over and would happily accept a freshly composed lookalike — the
    # exact regression (composing per event) this service exists to prevent.
    for frame, affect in zip(display.rendered, expected, strict=True):
        assert frame is service.face(affect)
    # Not a copy: the same object every time, which is the whole performance claim.
    await bus.publish(_affect_changed(affect=Affect.HAPPY))
    await display.wait_for_frames(4)
    assert display.rendered[3] is display.rendered[0]


# --- the artifact a human actually looks at ------------------------------------------------


def _blit(
    dst: bytearray, dst_width: int, src: DisplayFrame, *, at_x: int, at_y: int
) -> None:
    """Copy a frame into a larger buffer, one scanline slice at a time.

    Lives in the test, not in ``faces.py``: tiling is a convenience for eyeballing, and the
    robot never composes a contact sheet.
    """
    for row in range(src.height):
        src_off = row * src.width * 3
        dst_off = ((at_y + row) * dst_width + at_x) * 3
        dst[dst_off : dst_off + src.width * 3] = src.pixels[
            src_off : src_off + src.width * 3
        ]


async def test_writes_a_contact_sheet_of_a_whole_turn() -> None:
    """Drive a realistic turn through the bus and lay the frames out in render order.

    ``tests/core/test_faces.py`` already sheets ``render_face`` in isolation; this one is about
    the *path* — that a sequence of real events puts the right face on glass in the right
    order. Every assertion in this file can pass while the service renders a correct-looking
    set of frames in the wrong order, or maps ``THINKING`` to the confused face; only a human
    looking at the sheet catches that, which is why the ACs cannot be the last word here.

    A test rather than a script because CI uploads ``.artifacts/``, so a reviewer can open the
    sheet from the build. Rooted at the repo rather than the cwd so it lands in the same place
    however pytest was invoked, and given its own ``out_dir`` so the numbering never collides.
    """
    tile_w, tile_h = 240, 160
    out_dir = REPO_ROOT / ".artifacts" / "frames" / "expression"
    clock = FakeClock()
    bus = AsyncioEventBus()
    display = _RecordingDisplay(out_dir=out_dir, resolution=(tile_w, tile_h))
    service = _build(bus, display, clock)

    _register(bus, service)
    await bus.start()
    try:
        # A plausible turn: the operational states the robot walks through, with the model's
        # Tier-2 overlays landing between them. All eight affects, in the order they'd appear.
        #
        # Published one at a time, each awaited before the next. The two subscriptions are
        # separate bus queues dispatched concurrently, so firing all eight at once produces a
        # sheet whose tile order shuffles run to run — which makes the artifact useless for
        # spotting a genuine ordering bug. Serialising here is the test choosing a defined
        # order to depict, not the service promising one.
        script: tuple[StateTransitioned | AffectChanged, ...] = (
            _transitioned(to=RobotState.LISTENING),
            _affect_changed(affect=Affect.HAPPY),
            _transitioned(to=RobotState.THINKING),
            _affect_changed(affect=Affect.CONFUSED),
            _transitioned(to=RobotState.SPEAKING),
            _affect_changed(affect=Affect.SAD),
            _transitioned(to=RobotState.IDLE),
            _transitioned(to=RobotState.SLEEPING),
        )
        for index, event in enumerate(script, start=1):
            await bus.publish(event)
            await display.wait_for_frames(index)
    finally:
        await bus.stop()

    columns, rows = 4, 2
    sheet_w, sheet_h = tile_w * columns, tile_h * rows
    sheet = bytearray(sheet_w * sheet_h * 3)
    for index, frame in enumerate(display.rendered):
        _blit(
            sheet,
            sheet_w,
            frame,
            at_x=(index % columns) * tile_w,
            at_y=(index // columns) * tile_h,
        )
    await display.render(
        DisplayFrame(
            pixels=bytes(sheet), width=sheet_w, height=sheet_h, format="RGB888"
        )
    )

    assert display.frames_rendered == 9
    assert all(path.exists() and path.stat().st_size > 0 for path in display.frames)
