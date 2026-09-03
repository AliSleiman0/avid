"""Contract suite for the ``Drive`` port (#400, ADR-015, SDS §3.9.1, §3.9.5, §14.4).

A port's contract test runs against *every* adapter, so a fake can never quietly drift from
the real thing (P6). Parametrized over the hardware seam (:data:`FAKE_REAL_PARAMS`): the
``"fake"`` case runs everywhere; the ``"real"`` case skips off the Pi and, on the Pi, exercises
the L9110S adapter with the wheels **off the ground** — every run below is a short pulse at a
low duty, the bench's wiring-check speed, and none of them assume a floor.

The three assertions are the port's three promises (SDS §3.9.5): **signed speed in the robot
frame, milliseconds actually driven, and an instant idempotent stop.** They are made through
:class:`_IntrospectableDrive`, a local Protocol adding the observation points both adapters
carry (:attr:`is_running`) to the :class:`~avid.core.ports.Drive` port — off the port because
``DriveService`` commands the wheels and never reads them back.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

import pytest

from avid.adapters.drive import FakeDrive
from avid.core.hal import DriveCapabilities
from avid.core.ports import Drive

from ._hardware import FAKE_REAL_PARAMS, skip_off_pi

# A round number so the odometer arithmetic below is checkable by eye: 100 mm/s at full duty,
# so 0.5 duty for 400 ms is 20 mm. The shipped 128 mm/s is provisional (SPK-6) and would only
# obscure the relation the tests assert.
_CAPS = DriveCapabilities(mm_per_s_at_full=100.0)
_SPEED = 0.4  # the bench's wiring-check duty (SDS §3.9.5); a shared rail, not a race


@runtime_checkable
class _IntrospectableDrive(Drive, Protocol):
    """The ``Drive`` port plus the off-port observation point the contract asserts on."""

    @property
    def is_running(self) -> bool: ...


# --- shared contract: every Drive adapter must satisfy it ------------------------------------


@pytest.fixture(params=FAKE_REAL_PARAMS)
def drive(request: pytest.FixtureRequest) -> _IntrospectableDrive:
    """Every Drive adapter, real and fake, must satisfy the tests below (P6, SDS §14.4).

    The ``"real"`` case skips off the Pi; on the Pi it constructs the L9110S adapter on the
    measured pin map with the measured flip, so the same pulses are asserted on both."""
    if request.param == "fake":
        return FakeDrive(capabilities=_CAPS)
    skip_off_pi()
    # Imported here, not at module top: gpiozero is Pi-only (apt, via --system-site-packages,
    # ADR-008) and absent off the Pi, so only the on-Pi "real" branch ever touches it (P5).
    from avid.adapters.drive import L9110sDrive

    return L9110sDrive(
        capabilities=_CAPS,
        left_forward_pin=5,
        left_backward_pin=6,
        right_forward_pin=12,
        right_backward_pin=13,
        forward_is_inverted=True,
    )


def test_adapter_satisfies_the_drive_port(drive: _IntrospectableDrive) -> None:
    assert isinstance(drive, Drive)


def test_capabilities_report_what_the_rig_was_built_with(
    drive: _IntrospectableDrive,
) -> None:
    """§3.9.3: the adapter's own report is what the step planner negotiates against — asserted
    through the port, because that is the direction the dependency runs."""
    assert drive.capabilities == _CAPS


async def test_a_completed_run_reports_the_whole_duration(
    drive: _IntrospectableDrive,
) -> None:
    """``run()`` returns milliseconds **driven**. A run that completes drove all of them."""
    driven = await drive.run(_SPEED, _SPEED, duration_ms=200)
    assert driven == 200
    assert not drive.is_running


async def test_a_run_is_cancellable_and_reports_less_than_it_was_asked(
    drive: _IntrospectableDrive,
) -> None:
    """§3.9.5: a preempting cancel stops a run partway rather than after a monolithic sweep,
    and the motors are left stopped. The partial figure is what the service's odometer needs."""
    task = asyncio.create_task(drive.run(_SPEED, _SPEED, duration_ms=2000))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not drive.is_running


async def test_stop_ends_a_run_in_flight_and_the_run_reports_what_it_drove(
    drive: _IntrospectableDrive,
) -> None:
    """The edge-abort path: ``stop()`` is a direct call (§9.1.4) and the run it interrupts
    returns **fewer** milliseconds than it was asked for — never the full figure."""
    task = asyncio.create_task(drive.run(_SPEED, _SPEED, duration_ms=2000))
    await asyncio.sleep(0.05)
    await drive.stop()
    driven = await task
    assert 0 <= driven < 2000
    assert not drive.is_running


async def test_stop_is_idempotent_and_safe_when_idle(
    drive: _IntrospectableDrive,
) -> None:
    """Twice in a row, and with nothing running — an abort handler must not have to ask."""
    await drive.stop()
    await drive.stop()
    assert not drive.is_running
    # And a stop does not poison the next run.
    assert await drive.run(_SPEED, _SPEED, duration_ms=100) == 100


async def test_speeds_beyond_unity_are_clamped_not_rejected(
    drive: _IntrospectableDrive,
) -> None:
    """Clamping is the adapter's job (SDS §3.9.1's servo rule, applied to duty): a caller past
    the range gets full duty, not an exception mid-leg."""
    assert await drive.run(5.0, -5.0, duration_ms=60) == 60


async def test_the_reverse_direction_is_accepted_through_the_same_call(
    drive: _IntrospectableDrive,
) -> None:
    """``-`` is *away from the user*; the flip, if any, is the adapter's and invisible here."""
    assert await drive.run(-_SPEED, -_SPEED, duration_ms=60) == 60


# --- FakeDrive-specific: the trace and the odometer (SDS §14.3, §14.4) ------------------------


async def test_the_odometer_integrates_what_was_driven() -> None:
    """0.5 duty × 100 mm/s × 400 ms = 20 mm, and the same run backward brings it home."""
    fake = FakeDrive(capabilities=_CAPS)
    await fake.run(0.5, 0.5, duration_ms=400)
    assert fake.odometer_mm == pytest.approx(20.0)
    await fake.run(-0.5, -0.5, duration_ms=400)
    assert fake.odometer_mm == pytest.approx(0.0)


async def test_a_stopped_run_advances_the_odometer_only_as_far_as_it_got() -> None:
    """The reason ``run()`` returns ms driven: the odometer and the return value agree, and both
    say *less than asked* — the property the service's own odometer relies on."""
    fake = FakeDrive(capabilities=_CAPS)
    task = asyncio.create_task(fake.run(1.0, 1.0, duration_ms=2000))
    await asyncio.sleep(0.05)
    await fake.stop()
    driven = await task
    assert driven < 2000
    assert fake.odometer_mm == pytest.approx(driven / 1000 * 100.0)


async def test_the_trace_records_every_run_including_a_cancelled_one() -> None:
    """A cancelled run is recorded as partial rather than vanishing — the ``finally`` is what a
    test of "the motors were commanded, then stopped" reads."""
    fake = FakeDrive(capabilities=_CAPS)
    await fake.run(0.4, 0.4, duration_ms=100)
    task = asyncio.create_task(fake.run(0.4, 0.4, duration_ms=2000))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(fake.runs) == 2
    assert fake.runs[0] == (0.4, 0.4, 100)
    assert fake.runs[1][2] < 2000


async def test_a_pivot_moves_the_odometer_nowhere() -> None:
    """Equal and opposite wheels turn the robot on the spot; the forward-axis odometer must not
    credit it with travel. (The planner never emits a pivot; the fake still has to be honest.)"""
    fake = FakeDrive(capabilities=_CAPS)
    await fake.run(0.5, -0.5, duration_ms=400)
    assert fake.odometer_mm == pytest.approx(0.0)


async def test_a_rig_with_no_wheels_reports_none_and_records_the_call_it_should_never_get() -> (
    None
):
    """``capabilities is None`` is *no wheels* (§3.9.3): the planner plans nothing, so a run
    reaching this adapter is a service defect — recorded, not raised, so the test that catches
    it reads a trace rather than a traceback."""
    fake = FakeDrive(capabilities=None)
    assert fake.capabilities is None
    assert await fake.run(0.4, 0.4, duration_ms=40) == 40
    assert fake.runs == [(0.4, 0.4, 40)]
    assert fake.odometer_mm == 0.0


async def test_stops_are_counted() -> None:
    fake = FakeDrive(capabilities=_CAPS)
    assert fake.stops == 0
    await fake.stop()
    assert fake.stops == 1
