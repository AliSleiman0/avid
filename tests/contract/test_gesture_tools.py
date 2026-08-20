"""Contract suite for the ``GestureTools`` port (#204, SDS §6.6, §3.9.1, §3.9.3).

A port's contract test runs against *every* implementation, so a double can never quietly drift
from the real thing (P6: *"a port without a working simulator adapter is an incomplete port"*).

Like ``AffectTools``, this is a **tools** port rather than a device one, so there is no fake
adapter in ``adapters/`` at all: the real implementation is ``MotionService``, which satisfies
the Protocol structurally with no inheritance and no edit there. The two legs are a minimal
recording double — proving the *shape* is implementable by something that is not the service —
and the real service driving a ``FakeServo``. Both are constructible without hardware, so
neither leg skips.

What the port promises is narrow, and the narrowness is the safety property: **a direction, never
an angle**. The model states an intent and §3.9.3 decides what that means in degrees on this rig,
which is what keeps one tool working across the 2-servo robot, the 1-servo fallback and the fake.

⚠️ The **declines** are where a contract suite earns its keep here. `look_at` has three outcomes
and two of them are refusals; a double that only ever returned ``ACCEPTED`` would satisfy every
happy-path assertion and hide the two answers the model actually has to reason about.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest

from avid.adapters.clock import FakeClock
from avid.adapters.servo import FakeServo
from avid.core.event_bus import AsyncioEventBus
from avid.core.ports import GestureTools
from avid.domain import Axis, Direction, LookAtResult
from avid.services.motion import MotionService

# The rig config/*.toml declares (#200): pan ch0, tilt ch13.
_PAN = Axis(name="pan", channel=0, min_deg=30.0, max_deg=150.0)
_TILT = Axis(name="tilt", channel=13, min_deg=60.0, max_deg=120.0)

_COOLDOWN_MS = 4000


class _RecordingGestureTools:
    """A minimal ``GestureTools`` implementation that is *not* ``MotionService``.

    Present so the shared block proves the contract is satisfiable by the shape alone. If the
    port ever grew a promise only the service could keep, this leg would be the first to fail —
    which is what a contract suite is for."""

    def __init__(self) -> None:
        self.looks: list[tuple[Direction, UUID]] = []
        self._seen: set[Direction] = set()

    async def look_at(
        self, direction: Direction, *, correlation_id: UUID
    ) -> LookAtResult:
        self.looks.append((direction, correlation_id))
        # A crude cooldown of its own, so the double honours the port's *shape* — repeat calls
        # may be refused — without pretending to a servo's judgement about them.
        if direction in self._seen:
            return LookAtResult.COOLING_DOWN
        self._seen.add(direction)
        return LookAtResult.ACCEPTED


@pytest.fixture(params=["fake", "real"])
async def tools(request: pytest.FixtureRequest) -> AsyncIterator[GestureTools]:
    """Both implementations, and neither needs hardware or a key.

    The real leg needs a **started** bus: ``MotionService`` publishes ``motion.gesture_started``
    on the way into a gesture, and the bus refuses to publish before ``start()``. That is not
    incidental to this contract — an accepted ``look_at`` is one that *ends in movement*, and
    the events are how anything downstream learns it happened."""
    if request.param != "real":
        yield _RecordingGestureTools()
        return
    clock = FakeClock()
    bus = AsyncioEventBus()
    service = MotionService(
        bus=bus,
        servo=FakeServo(axes=(_PAN, _TILT)),
        clock=clock,
        idle_relax_ms=3000,
        look_at_cooldown_ms=_COOLDOWN_MS,
        # A drift band far longer than any test here, so idle motion cannot perturb a
        # cooldown assertion — this suite is about the port, not about #205.
        drift_interval_min_s=600.0,
        drift_interval_max_s=1200.0,
        drift_amplitude_frac=0.03,
    )
    await bus.start()
    try:
        yield service
    finally:
        await service.stop()
        await bus.stop()


def test_implementation_satisfies_the_gesture_tools_port(tools: GestureTools) -> None:
    assert isinstance(tools, GestureTools)


@pytest.mark.parametrize("direction", list(Direction), ids=lambda d: d.name.lower())
async def test_every_direction_is_accepted_on_a_two_axis_rig(
    tools: GestureTools, direction: Direction
) -> None:
    """The five directions are the port's whole vocabulary, and the target rig can express all
    of them — which is the point of ADR-009's second servo.

    Parametrised over the domain enum rather than a hand-written list, so a sixth direction is
    covered here the moment it exists."""
    assert (
        await tools.look_at(direction, correlation_id=uuid4()) is LookAtResult.ACCEPTED
    )


async def test_correlation_id_is_required_and_keyword_only(tools: GestureTools) -> None:
    """The same signature discipline ``AffectTools`` has, asserted rather than left to a
    docstring: a movement with no turn behind it is a fact nothing can be traced back to
    (§3.12.2 — the id is *propagated*, never minted downstream)."""
    with pytest.raises(TypeError):
        await tools.look_at(Direction.LEFT, uuid4())  # type: ignore[misc]


async def test_a_repeat_request_is_refused_rather_than_performed(
    tools: GestureTools,
) -> None:
    """⚠️ **Rate-limiting is part of this port's contract, not an implementation detail.**

    A model that decides gesturing is delightful would otherwise drive both servos continuously
    — contradicting the gate's *"relaxes when idle"* clause by construction and putting sustained
    load on the rail #206 is measuring. So an implementation that never refuses is not a valid
    one, and this is the assertion that says so."""
    first = await tools.look_at(Direction.LEFT, correlation_id=uuid4())
    second = await tools.look_at(Direction.LEFT, correlation_id=uuid4())

    assert first is LookAtResult.ACCEPTED
    assert second is LookAtResult.COOLING_DOWN


async def test_the_result_is_never_none(tools: GestureTools) -> None:
    """Declining is a first-class outcome, which is why this returns a value at all.

    §6.6 classifies the tool fire-and-forget, so the model never learns whether the servo
    *arrived* — but it must learn whether the robot **tried**. A ``None`` return (the shape
    ``AffectTools`` deliberately has) would make a refusal indistinguishable from a success."""
    result = await tools.look_at(Direction.CENTER, correlation_id=uuid4())
    assert isinstance(result, LookAtResult)


# --- the real service's own promises -----------------------------------------


async def _service(
    *axes: Axis,
) -> tuple[MotionService, FakeServo, AsyncioEventBus, FakeClock]:
    clock = FakeClock()
    bus = AsyncioEventBus()
    servo = FakeServo(axes=axes)
    service = MotionService(
        bus=bus,
        servo=servo,
        clock=clock,
        idle_relax_ms=3000,
        look_at_cooldown_ms=_COOLDOWN_MS,
        # A drift band far longer than any test here, so idle motion cannot perturb a
        # cooldown assertion — this suite is about the port, not about #205.
        drift_interval_min_s=600.0,
        drift_interval_max_s=1200.0,
        drift_amplitude_frac=0.03,
    )
    await bus.start()
    return service, servo, bus, clock


async def test_an_accepted_look_actually_moves_the_rig() -> None:
    """The half only ``MotionService`` can keep — and the one a recording double cannot fake.

    ``left`` has to *mean* something: a trace ending toward the pan axis's maximum, on channel
    0. The port could be satisfied by an implementation that returned ``ACCEPTED`` and moved
    nothing, which is precisely the silent failure #207 exists to catch by eye."""
    service, servo, bus, _clock = await _service(_PAN, _TILT)
    try:
        assert (
            await service.look_at(Direction.LEFT, correlation_id=uuid4())
            is LookAtResult.ACCEPTED
        )
        task = service._task
        assert task is not None
        await asyncio.wait_for(task, 2.0)
    finally:
        await service.stop()
        await bus.stop()

    channels = {channel for channel, _ in servo.moves}
    assert channels == {_PAN.channel}
    # Increasing degrees is LEFT on a pan axis (domain/motion.py's stated convention), so the
    # head ends above centre. Asserting the direction rather than the exact angle: the angle is
    # plan()'s business and is pinned there.
    assert servo.moves[-1][1] > _PAN.centre_deg


async def test_a_direction_the_rig_has_no_axis_for_is_declined_honestly() -> None:
    """AC-8, and the reason it is not a silent no-op.

    ``up`` on a pan-only robot cannot be performed. Returning ``ACCEPTED`` would leave the model
    believing it moved and describing motion that never happened; a bare ``False`` would let it
    apologise for a malfunction. ``NO_AXIS`` is the third thing: the robot working correctly and
    able to say what it cannot do."""
    service, servo, bus, _clock = await _service(_PAN)
    try:
        outcome = await service.look_at(Direction.UP, correlation_id=uuid4())
    finally:
        await service.stop()
        await bus.stop()

    assert outcome is LookAtResult.NO_AXIS
    assert servo.moves == []


async def test_a_declined_call_does_not_extend_its_own_cooldown() -> None:
    """⚠️ The cooldown advances only on **acceptance**.

    Otherwise a model retrying politely locks itself out for as long as it keeps asking — the
    rate limit becomes a trap that punishes exactly the behaviour it is trying to encourage.

    ⚠️ **The clock has to move between the accept and the refusal, or this test cannot fail.**
    An earlier version refused immediately after accepting, so the two calls shared a monotonic
    instant and a service that *did* advance on refusal set the deadline to the same value —
    invisible. Found by neutering the rule and watching this stay green. Half the window is
    spent before the refusal, so a refusal that reset the timer would push the third call out
    past its proper moment."""
    service, _servo, bus, clock = await _service(_PAN, _TILT)
    half = _COOLDOWN_MS / 1000 / 2
    try:
        await service.look_at(Direction.LEFT, correlation_id=uuid4())  # accepted at t=0

        await clock.advance(half)  # t = half: still inside the window
        assert (
            await service.look_at(Direction.RIGHT, correlation_id=uuid4())
            is LookAtResult.COOLING_DOWN
        )

        await clock.advance(half)  # t = the full window from the ACCEPTED call
        assert (
            await service.look_at(Direction.RIGHT, correlation_id=uuid4())
            is LookAtResult.ACCEPTED
        ), "the refused call extended its own cooldown"
    finally:
        await service.stop()
        await bus.stop()


async def test_the_cooldown_is_measured_on_the_monotonic_clock() -> None:
    """§9.1.1: wall clock for humans, monotonic for arithmetic.

    A cooldown measured on ``timestamp_ms`` would be **skipped entirely** by an NTP step
    forward — and the Pi corrects its clock seconds after boot, which is exactly when a first
    conversation is likely to be happening. Driving the wall clock alone must not open the gate.
    """
    service, _servo, bus, clock = await _service(_PAN, _TILT)
    try:
        await service.look_at(Direction.LEFT, correlation_id=uuid4())
        clock.step_wall_clock(
            3600
        )  # an hour of NTP correction, no monotonic time passing
        assert (
            await service.look_at(Direction.RIGHT, correlation_id=uuid4())
            is LookAtResult.COOLING_DOWN
        )
    finally:
        await service.stop()
        await bus.stop()
