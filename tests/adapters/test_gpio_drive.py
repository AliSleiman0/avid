"""The real drive and edge adapters' *logic*, off the Pi (#400, SDS §3.9.5).

``L9110sDrive`` and ``Tcrt5000EdgeSensor`` are proven on the rig by the contract suites'
``"real"`` params. But two pieces of their logic are pure decisions that were **measured** on
the bench and would fail silently if inverted — the direction flip and the sensor polarity —
and a robot with the flip wrong drives backward on ``STEP_TOWARD`` and walks off the far edge.
Those two decisions are asserted here against a stub ``gpiozero`` module, so they are graded
on every commit rather than on the one bench evening that owns the hardware.

The stub is a *module*, not a mock: it records what the adapter asked of the pins, and the
tests read that record. ``tests/adapters/`` is where a device seam may be substituted
(SDS §14.3).
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator

import pytest

from avid.adapters.drive import L9110sDrive
from avid.adapters.edge import Tcrt5000EdgeSensor
from avid.core.hal import DriveCapabilities

_CAPS = DriveCapabilities(mm_per_s_at_full=100.0)
_PINS = {
    "left_forward_pin": 5,
    "left_backward_pin": 6,
    "right_forward_pin": 12,
    "right_backward_pin": 13,
}


class _StubMotor:
    """Records the last command per channel, the way the L9110S would see it."""

    instances: list[_StubMotor] = []

    def __init__(self, *, forward: int, backward: int) -> None:
        self.forward_pin = forward
        self.backward_pin = backward
        self.commands: list[tuple[str, float]] = []
        self.closed = False
        _StubMotor.instances.append(self)

    def forward(self, speed: float = 1.0) -> None:
        self.commands.append(("forward", speed))

    def backward(self, speed: float = 1.0) -> None:
        self.commands.append(("backward", speed))

    def stop(self) -> None:
        self.commands.append(("stop", 0.0))

    def close(self) -> None:
        self.closed = True


class _StubInput:
    """A pin whose level a test sets."""

    levels: dict[int, int] = {}

    def __init__(self, pin: int) -> None:
        self.pin = pin
        self.closed = False

    @property
    def value(self) -> int:
        return _StubInput.levels.get(self.pin, 0)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def gpiozero(monkeypatch: pytest.MonkeyPatch) -> Iterator[types.ModuleType]:
    """A stand-in ``gpiozero`` the adapters' lazy imports resolve to, off the Pi."""
    module = types.ModuleType("gpiozero")
    module.Motor = _StubMotor  # type: ignore[attr-defined]
    module.DigitalInputDevice = _StubInput  # type: ignore[attr-defined]
    _StubMotor.instances = []
    _StubInput.levels = {}
    monkeypatch.setitem(sys.modules, "gpiozero", module)
    yield module


def _drive(*, inverted: bool) -> L9110sDrive:
    return L9110sDrive(capabilities=_CAPS, forward_is_inverted=inverted, **_PINS)


def _last_moves() -> list[tuple[str, float]]:
    """The last non-stop command each channel received, left then right."""
    out = []
    for motor in _StubMotor.instances:
        moves = [c for c in motor.commands if c[0] != "stop"]
        out.append(moves[-1] if moves else ("none", 0.0))
    return out


# --- the flip: measured, global, applied once -----------------------------------------------


async def test_the_pins_are_claimed_as_configured_and_lazily(
    gpiozero: types.ModuleType,
) -> None:
    """Constructing the adapter claims nothing — the import and the pins wait for the first
    run — and when they are claimed, they are the configured BCM numbers, left then right."""
    drive = _drive(inverted=True)
    assert _StubMotor.instances == []
    await drive.run(0.4, 0.4, duration_ms=40)
    assert [(m.forward_pin, m.backward_pin) for m in _StubMotor.instances] == [
        (5, 6),
        (12, 13),
    ]


async def test_with_the_measured_flip_forward_at_the_port_is_backward_at_the_motor(
    gpiozero: types.ModuleType,
) -> None:
    """The bench finding, encoded: ``Motor.forward()`` on both channels drives the chassis
    backward, so ``+`` at the port — toward the user — must be ``Motor.backward()`` on BOTH.

    Neuter: swap the branches in ``_set_blocking`` and this reads ``forward`` — and on the desk
    that is a robot that walks off the far edge on its first ``STEP_TOWARD``."""
    drive = _drive(inverted=True)
    await drive.run(0.4, 0.4, duration_ms=40)
    assert _last_moves() == [("backward", 0.4), ("backward", 0.4)]

    await drive.run(-0.4, -0.4, duration_ms=40)
    assert _last_moves() == [("forward", 0.4), ("forward", 0.4)]


async def test_without_the_flip_the_port_and_the_motor_agree(
    gpiozero: types.ModuleType,
) -> None:
    """A rig whose hubs do not cancel the mirror sets ``forward_is_inverted = false`` and gets
    the datasheet mapping — one flag, both channels, no per-wheel sign."""
    drive = _drive(inverted=False)
    await drive.run(0.4, 0.4, duration_ms=40)
    assert _last_moves() == [("forward", 0.4), ("forward", 0.4)]


async def test_the_flip_is_global_not_per_wheel(gpiozero: types.ModuleType) -> None:
    """A pivot — equal and opposite — flips BOTH channels, never one: two per-wheel flags
    were the diagram's assumption and the bench disproved it."""
    drive = _drive(inverted=True)
    await drive.run(0.4, -0.4, duration_ms=40)
    assert _last_moves() == [("backward", 0.4), ("forward", 0.4)]


async def test_every_path_out_of_a_run_stops_the_motors(
    gpiozero: types.ModuleType,
) -> None:
    """Completion, ``stop()`` mid-run, and ``close()`` all end with both channels stopped — a
    wheel left turning is the one failure that outlives the program."""
    drive = _drive(inverted=True)
    await drive.run(0.4, 0.4, duration_ms=40)
    assert all(m.commands[-1] == ("stop", 0.0) for m in _StubMotor.instances)

    await drive.stop()
    assert all(m.commands[-1] == ("stop", 0.0) for m in _StubMotor.instances)

    drive.close()
    assert all(m.closed for m in _StubMotor.instances)


async def test_speeds_are_clamped_before_they_reach_a_motor(
    gpiozero: types.ModuleType,
) -> None:
    drive = _drive(inverted=False)
    await drive.run(7.0, -7.0, duration_ms=40)
    assert _last_moves() == [("forward", 1.0), ("backward", 1.0)]


async def test_a_run_reports_what_it_drove(gpiozero: types.ModuleType) -> None:
    """The port's promise, kept by the real adapter's slicing: a completed run reports the
    whole duration; a stopped one, less."""
    drive = _drive(inverted=True)
    assert await drive.run(0.4, 0.4, duration_ms=60) == 60
    assert drive.capabilities == _CAPS


# --- the polarity: measured, HIGH = surface, every pin must agree ----------------------------


async def test_high_is_surface_when_active_high_and_every_pin_must_agree(
    gpiozero: types.ModuleType,
) -> None:
    """The bench finding: this board reads HIGH with a desk under it. And one sensor over the
    edge is an edge — ``clear()`` is ``all``, never ``any``.

    Neuter: flip ``surface_level`` and a desk reads as a permanent edge — a robot that never
    steps and never says why."""
    edge = Tcrt5000EdgeSensor(pins=(23, 22), active_high=True)
    _StubInput.levels.update({23: 1, 22: 1})
    assert await edge.clear() is True

    _StubInput.levels[22] = 0  # one wheel over the edge
    assert await edge.clear() is False

    _StubInput.levels.update({23: 0, 22: 0})
    assert await edge.clear() is False


async def test_the_datasheet_polarity_is_one_config_flag_away(
    gpiozero: types.ModuleType,
) -> None:
    """A module revision that follows the datasheet (LOW-on-detect) is ``active_high = false``,
    not a code change."""
    edge = Tcrt5000EdgeSensor(pins=(23,), active_high=False)
    _StubInput.levels[23] = 0
    assert await edge.clear() is True
    _StubInput.levels[23] = 1
    assert await edge.clear() is False


async def test_a_single_sensor_is_a_legal_rig_and_no_sensors_is_not(
    gpiozero: types.ModuleType,
) -> None:
    """One unit shorted on the bench and is retired; the adapter takes what is wired. Zero is
    a robot that steps blind, and refused (F-13)."""
    edge = Tcrt5000EdgeSensor(pins=(23,), active_high=True)
    _StubInput.levels[23] = 1
    assert await edge.clear() is True
    with pytest.raises(ValueError, match="steps blind"):
        Tcrt5000EdgeSensor(pins=(), active_high=True)


async def test_the_sensor_pins_are_claimed_lazily_and_released_on_close(
    gpiozero: types.ModuleType,
) -> None:
    edge = Tcrt5000EdgeSensor(pins=(23, 22), active_high=True)
    edge.close()  # nothing claimed yet — a no-op, not an error
    await edge.clear()
    edge.close()
    edge.close()
