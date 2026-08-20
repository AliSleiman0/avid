"""Contract suite for the ``Servo`` port (AVID-52, SDS §3.9.1, §3.9.3, §14.4).

A port's contract test runs against *every* adapter, so a fake can never quietly drift
from the real thing (P6). The shared tier is parametrized over the M2.0 hardware seam
(:data:`FAKE_REAL_PARAMS`): the ``"fake"`` case runs everywhere; the ``"real"`` case
skips off the Pi and, on the Pi, exercises the real :class:`Pca9685Servo` — the same seam
``test_camera.py`` / ``test_display.py`` use.

The three assertions are the SDS §14.4 trio — **clamp, cancel, relax** — the servo's port
promises (SDS §3.9.1). They are made through :class:`_IntrospectableServo`, a local Protocol
that adds the two observation points both adapters carry (:meth:`position`,
:meth:`is_energised`) to the :class:`~avid.core.ports.Servo` port. Those two are deliberately
*off* the port — ``MotionService`` commands the servo and never reads it back, so they are
not an application need — exactly as ``FakeCamera.captures`` is off the ``Camera`` port. The
clamp limit itself comes from the port's own :attr:`~avid.core.ports.Servo.axes`.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

import pytest

from avid.adapters.servo import FakeServo
from avid.core.hal import Axis
from avid.core.ports import Servo

from ._hardware import FAKE_REAL_PARAMS, skip_off_pi

# The pan+tilt rig config/*.toml declares since #200 (ADR-009, SDS §3.9.4): ch0 body turn,
# ch13 head, with *different* reaches — which is the point. The clamp test drives past max_deg.
#
# ⚠️ Two axes with two different reaches, not two with the same one. A suite that clamped both
# to 0–180 would pass against an adapter that keyed its limits by anything at all — a shared
# limit, the first axis's, the last one's — because every answer would be identical. The
# asymmetry is what makes "clamps per axis" a claim a test can fail.
_CHANNEL = 0
_MIN_DEG, _MAX_DEG = 30.0, 150.0
_TILT_CHANNEL = 13
_TILT_MIN_DEG, _TILT_MAX_DEG = 60.0, 120.0
_AXES = (
    Axis(name="pan", channel=_CHANNEL, min_deg=_MIN_DEG, max_deg=_MAX_DEG),
    Axis(
        name="tilt",
        channel=_TILT_CHANNEL,
        min_deg=_TILT_MIN_DEG,
        max_deg=_TILT_MAX_DEG,
    ),
)


@runtime_checkable
class _IntrospectableServo(Servo, Protocol):
    """The ``Servo`` port plus the two off-port observation points the contract asserts on.

    Both adapters satisfy it structurally, so the shared fixture is typed against it and
    ``mypy --strict`` stays clean without widening the real port (SDS §3.9.1)."""

    def position(self, channel: int) -> float: ...

    def is_energised(self, channel: int) -> bool: ...


# --- shared contract: every Servo adapter must satisfy it --------------------


@pytest.fixture(params=FAKE_REAL_PARAMS)
def servo(request: pytest.FixtureRequest) -> _IntrospectableServo:
    """Every Servo adapter, real and fake, must satisfy the tests below (P6, SDS §14.4).

    The ``"real"`` case skips off the Pi via :func:`skip_off_pi`; on the Pi it constructs
    :class:`Pca9685Servo` with the same axes the fake gets, so clamp/cancel/relax are
    asserted identically. This replaces the placeholder skip the seam (AVID-50) laid."""
    if request.param == "fake":
        return FakeServo(axes=_AXES)
    skip_off_pi()
    # Imported here, not at module top: adafruit_servokit is Pi-only and absent off the Pi,
    # so only the on-Pi "real" branch ever touches it (P5, ADR-008).
    from avid.adapters.servo import Pca9685Servo

    return Pca9685Servo(
        axes=_AXES,
        i2c_address=0x40,
        min_pulse_us=500,
        max_pulse_us=2500,
        freq_hz=50,
    )


def test_adapter_satisfies_the_servo_port(servo: _IntrospectableServo) -> None:
    assert isinstance(servo, Servo)


async def test_clamps_beyond_limits(servo: _IntrospectableServo) -> None:
    """§3.9.1: clamping is the ADAPTER's job, not the caller's. A command past the axis's
    reach lands within it — the limit read from the port's own ``axes``."""
    axis = next(a for a in servo.axes if a.channel == _CHANNEL)
    await servo.move_to(_CHANNEL, angle_deg=400, duration_ms=100)
    assert axis.min_deg <= servo.position(_CHANNEL) <= axis.max_deg


async def test_move_is_cancellable(servo: _IntrospectableServo) -> None:
    """§3.9.1: a preempting gesture must cancel an in-flight move — it stops partway rather
    than running to completion after the cancel."""
    task = asyncio.create_task(servo.move_to(_CHANNEL, 90, duration_ms=2000))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert servo.position(_CHANNEL) < 90  # never reached the target


async def test_relax_deenergises(servo: _IntrospectableServo) -> None:
    """After ``relax`` the channel is de-energized — M9's "no buzz when idle" gate."""
    await servo.move_to(_CHANNEL, 45, duration_ms=100)
    assert servo.is_energised(_CHANNEL)
    await servo.relax(_CHANNEL)
    assert not servo.is_energised(_CHANNEL)


# --- multi-axis: the 2 DoF rig ADR-009 accepted (#200, SDS §3.9.4) -----------


def test_axes_reports_the_whole_rig(servo: _IntrospectableServo) -> None:
    """§3.9.3: the adapter's own report is the inventory the gesture engine negotiates against.

    Asserted through the port rather than against the constructor argument, because that is the
    direction the dependency actually runs: ``MotionService`` asks *"do I have a tilt axis?"* and
    plans a real nod or a degraded pan wiggle on the answer. An adapter that accepted two axes
    and reported one would silently make the fallback the only path anyone ever sees."""
    assert [(axis.name, axis.channel) for axis in servo.axes] == [
        ("pan", _CHANNEL),
        ("tilt", _TILT_CHANNEL),
    ]


async def test_a_command_past_each_axiss_max_lands_on_that_axiss_max(
    servo: _IntrospectableServo,
) -> None:
    """The clamp is per axis, and the two reaches are deliberately different (SDS §3.9.1).

    A head binds against its bracket sooner than a body turns against its cable, so tilt is the
    narrower axis on this rig. Both channels are driven past their own maxima and each must land
    on *its* limit — 150° and 120°. That is a claim which fails if an adapter keys its limits by
    anything other than the channel: a shared limit, the first axis's, or the last one's. With
    two identical reaches every one of those wrong answers would look right."""
    assert _MAX_DEG != _TILT_MAX_DEG, "the asymmetry is what lets this test fail"

    await servo.move_to(_CHANNEL, angle_deg=400, duration_ms=60)
    await servo.move_to(_TILT_CHANNEL, angle_deg=400, duration_ms=60)

    assert servo.position(_CHANNEL) == _MAX_DEG
    assert servo.position(_TILT_CHANNEL) == _TILT_MAX_DEG


async def test_relax_de_energises_one_channel_and_leaves_the_other(
    servo: _IntrospectableServo,
) -> None:
    """Relax is addressed to a channel, not to the rig.

    #203's I²C-fault path relaxes *every* channel deliberately, and #205's micro-motion relaxes
    the one it just drifted — both need this to be a per-channel act. An adapter that cut every
    pulse on any relax would make the fault path look correct and the idle path look like a
    robot that goes limp mid-gesture."""
    await servo.move_to(_CHANNEL, 90.0, duration_ms=60)
    await servo.move_to(_TILT_CHANNEL, 90.0, duration_ms=60)
    assert servo.is_energised(_CHANNEL) and servo.is_energised(_TILT_CHANNEL)

    await servo.relax(_TILT_CHANNEL)

    assert servo.is_energised(_CHANNEL), "relaxing tilt de-energised pan as well"
    assert not servo.is_energised(_TILT_CHANNEL)


async def test_both_channels_start_relaxed_and_inside_their_reach(
    servo: _IntrospectableServo,
) -> None:
    """No buzz at boot, on every channel — and no channel parked outside its own limits.

    The second half matters more than it looks: a rig whose axes start at a shared 0° would have
    the tilt servo held 60° below its bracket's floor from the moment the process starts, which
    is a stall the gate would hear as a hum and diagnose as the relax timer."""
    for axis in servo.axes:
        assert not servo.is_energised(axis.channel), axis.name
        assert axis.min_deg <= servo.position(axis.channel) <= axis.max_deg, axis.name


# --- FakeServo-specific: the recorded movement trace (SDS §14.3, §14.4) ------


async def test_move_records_a_trace_ending_at_target() -> None:
    """The fake records each commanded step; the last entry is the (clamped) target, so a
    behaviour test asserts "the head ended up at 45°" rather than mocking the call."""
    fake = FakeServo(axes=_AXES)
    await fake.move_to(_CHANNEL, 45, duration_ms=100)
    assert fake.moves  # non-empty trace
    assert fake.moves[-1] == (_CHANNEL, 45.0)
    assert fake.position(_CHANNEL) == 45.0


async def test_trace_is_partial_when_the_move_is_cancelled() -> None:
    """A cancelled move leaves a partial trace, never a jump to the target — proof the
    steps really are separate awaits, not one monolithic sweep."""
    fake = FakeServo(axes=_AXES)
    task = asyncio.create_task(fake.move_to(_CHANNEL, 180, duration_ms=2000))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.moves  # some steps happened
    assert fake.moves[-1][1] < 180.0  # but not all the way there


async def test_move_clamps_the_recorded_trace() -> None:
    """The trace itself never exceeds the axis limits — the clamp is applied before stepping,
    so no over-limit angle is ever "commanded"."""
    fake = FakeServo(axes=_AXES)
    await fake.move_to(_CHANNEL, angle_deg=400, duration_ms=100)
    assert all(_MIN_DEG <= angle <= _MAX_DEG for _, angle in fake.moves)


def test_channels_start_relaxed() -> None:
    """No buzz at boot: a freshly built fake is de-energized on every channel."""
    fake = FakeServo(axes=_AXES)
    assert not fake.is_energised(_CHANNEL)
