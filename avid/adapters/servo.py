"""Servo adapters — the fake that *is* the simulator, and the real PCA9685 servo (AVID-52).

Two implementations of the :class:`~avid.core.ports.Servo` port, both behind the one
contract suite (P6, SDS §14.4). The port makes three promises — **clamp, cancel, relax** —
and both adapters must keep them identically (SDS §3.9.1):

* :class:`FakeServo` records an assertable movement trace and *is* the simulator, so the
  sim can never drift from the real servo — it is the real servo with no hardware behind
  it. Stdlib only.
* :class:`Pca9685Servo` drives a PCA9685 over I2C via ``adafruit-circuitpython-servokit``.
  That library is apt/pip-on-Pi and absent off it (ADR-008, like ``picamera2``), so it is
  imported **only** inside this module and **lazily**, inside the worker-thread helper —
  the module itself imports cleanly on CI and a laptop, where the fake path and mypy still
  need it to load (P5).

Two invariants both adapters share:

* **Clamping is the adapter's job, not the caller's** (SDS §3.9.1). The safe limit is a
  property of the physical linkage, so it is enforced at the lowest layer — a future second
  caller cannot bypass it.
* **A move is genuinely cancellable.** A preempting gesture cancels an in-flight
  :meth:`move_to`; the move steps toward its target across many awaits, so a
  ``CancelledError`` stops it partway rather than after a monolithic sweep.

``position``/``is_energised`` are adapter-level introspection, deliberately **off** the
``Servo`` port: ``MotionService`` commands the servo and never reads it back, so they are
not an application need (they are the contract suite's observation points, exactly as
``FakeCamera.captures`` is off the ``Camera`` port). A real servo has no position feedback,
so :meth:`position` reports the last *commanded* angle. Constructed only by the composition
root or a test fixture (P3); everything else depends on the port, not these classes (P2).
"""

from __future__ import annotations

import asyncio
from typing import Any

from avid.core.hal import Axis

# Angle update cadence for a smooth sweep. ~20 ms/step (≈50 Hz) is finer than a servo's
# own response and coarse enough that a 100 ms move is ~5 awaited steps — each an
# opportunity to observe a cancel and, on the real adapter, a thread-hopped hardware write.
_STEP_MS = 20


def _clamp(angle_deg: float, axis: Axis) -> float:
    """Clamp *angle_deg* to *axis*'s safe reach (SDS §3.9.1 — the adapter's job)."""
    return max(axis.min_deg, min(axis.max_deg, angle_deg))


def _steps(duration_ms: int) -> int:
    """Number of intermediate updates for a move of *duration_ms* (at least one)."""
    return max(1, duration_ms // _STEP_MS)


class FakeServo:
    """The :class:`~avid.core.ports.Servo` fake (P6): a recorded movement trace, no hardware.

    ``axes`` is injected (P7) and defines :attr:`axes` and the per-channel clamp limits. The
    public :attr:`moves` list is the assertable trace — one ``(channel, angle)`` entry per
    commanded step — so a test asserts *"the head ended up at 45°"* rather than mocking the
    call (SDS §14.3, §14.4). :meth:`move_to` steps toward its (clamped) target across awaited
    sleeps, so a preempting :meth:`asyncio.Task.cancel` genuinely stops it partway.
    """

    def __init__(self, *, axes: tuple[Axis, ...]) -> None:
        self._axes = axes
        self._by_channel = {axis.channel: axis for axis in axes}
        # Every channel starts at its min_deg, de-energised (no buzz at boot).
        self._position: dict[int, float] = {axis.channel: axis.min_deg for axis in axes}
        self._energised: dict[int, bool] = {axis.channel: False for axis in axes}
        # The assertable record of every commanded step, in order.
        self.moves: list[tuple[int, float]] = []

    @property
    def axes(self) -> tuple[Axis, ...]:
        """The axes this rig exposes, for negotiation (SDS §3.9.3 / ADR-009)."""
        return self._axes

    async def move_to(
        self, channel: int, angle_deg: float, *, duration_ms: int
    ) -> None:
        """Step *channel* smoothly to the clamped *angle_deg* over *duration_ms*.

        Clamps to the channel's :class:`~avid.core.hal.Axis` limits first (the adapter's job,
        SDS §3.9.1), then walks there in even steps, recording each and sleeping between them
        so the move is cancellable — a ``CancelledError`` raised mid-sleep propagates and the
        trace is left partial. The channel is energised for the duration and stays energised
        until :meth:`relax`.
        """
        target = _clamp(angle_deg, self._by_channel[channel])
        start = self._position[channel]
        self._energised[channel] = True
        n = _steps(duration_ms)
        step_dt = duration_ms / n / 1000
        for i in range(1, n + 1):
            await asyncio.sleep(step_dt)
            self._position[channel] = start + (target - start) * i / n
            self.moves.append((channel, self._position[channel]))

    async def relax(self, channel: int) -> None:
        """De-energize the channel (SDS §3.9.1) — the "no buzz when idle" promise (M9)."""
        self._energised[channel] = False

    def position(self, channel: int) -> float:
        """Last commanded angle for *channel* (introspection, off the port)."""
        return self._position[channel]

    def is_energised(self, channel: int) -> bool:
        """Whether *channel* is currently energised (introspection, off the port)."""
        return self._energised[channel]


class Pca9685Servo:
    """The real :class:`~avid.core.ports.Servo` on the Pi, driving a PCA9685 over I2C.

    ⚠️ **R-04 power (PMP §9.2, SPK-4).** The servo MUST be powered from a **separate 5 V
    rail, common ground only** — never the Pi's 5 V pin. A stalled servo's stall current can
    brown out the Pi and corrupt the SD card. This adapter commands motion; it cannot enforce
    the wiring, so the assumption is stated here and verified physically at bring-up (#57).

    ``adafruit-circuitpython-servokit`` is imported lazily inside :meth:`_write_blocking`
    (P5, ADR-008): it is Pi-only and absent off the Pi, so keeping it out of module scope
    lets this module load everywhere — the fake path, mypy, and the composition-root import
    all work off-Pi. Each angle write is blocking I2C, so it runs on a worker thread (P8),
    and the sweep is many small awaited writes so a preempting gesture cancels it (a running
    ``to_thread`` cannot itself be interrupted). :meth:`position` reports the last *commanded*
    angle — a servo has no feedback — and :meth:`is_energised` tracks whether the last command
    left a pulse on the channel.
    """

    def __init__(
        self,
        *,
        axes: tuple[Axis, ...],
        i2c_address: int,
        min_pulse_us: int,
        max_pulse_us: int,
        freq_hz: int,
    ) -> None:
        self._axes = axes
        self._by_channel = {axis.channel: axis for axis in axes}
        self._i2c_address = i2c_address
        self._min_pulse_us = min_pulse_us
        self._max_pulse_us = max_pulse_us
        self._freq_hz = freq_hz
        # The ServoKit handle, built on first use. Untyped (Any) because the library ships
        # no stubs (mypy resolves it via ignore_missing_imports).
        self._kit: Any | None = None
        self._position: dict[int, float] = {axis.channel: axis.min_deg for axis in axes}
        self._energised: dict[int, bool] = {axis.channel: False for axis in axes}

    @property
    def axes(self) -> tuple[Axis, ...]:
        """The axes this rig exposes, for negotiation (SDS §3.9.3 / ADR-009)."""
        return self._axes

    async def move_to(
        self, channel: int, angle_deg: float, *, duration_ms: int
    ) -> None:
        """Step *channel* to the clamped *angle_deg* over *duration_ms*, cancellably (P8).

        Clamps first (SDS §3.9.1), then walks to the target in even steps; each step's I2C
        write hops to a worker thread and the awaited sleep between steps is where a
        preempting gesture's cancel takes effect. The channel stays energised until
        :meth:`relax`.
        """
        target = _clamp(angle_deg, self._by_channel[channel])
        start = self._position[channel]
        n = _steps(duration_ms)
        step_dt = duration_ms / n / 1000
        for i in range(1, n + 1):
            await asyncio.sleep(step_dt)
            angle = start + (target - start) * i / n
            await asyncio.to_thread(self._write_blocking, channel, angle)
            self._position[channel] = angle
            self._energised[channel] = True

    async def relax(self, channel: int) -> None:
        """Cut the pulse on *channel* (``angle = None``) so the servo de-energizes (no buzz)."""
        await asyncio.to_thread(self._relax_blocking, channel)
        self._energised[channel] = False

    def _kit_blocking(self) -> Any:
        # Lazy, Pi-only import — kept out of module scope so this file loads off-Pi
        # (P5, ADR-008). The library has no stubs; mypy resolves it via override.
        if self._kit is None:
            from adafruit_servokit import ServoKit

            kit = ServoKit(channels=16, address=self._i2c_address)
            for axis in self._axes:
                servo = kit.servo[axis.channel]
                servo.set_pulse_width_range(self._min_pulse_us, self._max_pulse_us)
                servo.actuation_range = axis.max_deg
            self._kit = kit
        return self._kit

    def _write_blocking(self, channel: int, angle: float) -> None:
        self._kit_blocking().servo[channel].angle = angle

    def _relax_blocking(self, channel: int) -> None:
        # ServoKit de-energizes a channel when its angle is set to None (no pulse).
        self._kit_blocking().servo[channel].angle = None

    def position(self, channel: int) -> float:
        """Last commanded angle for *channel* (a servo has no feedback; off the port)."""
        return self._position[channel]

    def is_energised(self, channel: int) -> bool:
        """Whether *channel*'s last command left a pulse on it (off the port)."""
        return self._energised[channel]
