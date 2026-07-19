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

# One-servo pan rig, matching config/*.toml's [servo]. The clamp test drives past max_deg.
_CHANNEL = 0
_MIN_DEG, _MAX_DEG = 0.0, 180.0
_AXES = (Axis(name="pan", channel=_CHANNEL, min_deg=_MIN_DEG, max_deg=_MAX_DEG),)


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
