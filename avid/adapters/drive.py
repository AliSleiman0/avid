"""Drive adapters — the fake that *is* the simulator, and the real L9110S on GPIO (#400).

Implementations of the :class:`~avid.core.ports.Drive` port, behind one contract suite (P6,
SDS §14.4). The port makes three promises — **signed speed in the robot frame, milliseconds
actually driven, and an instant idempotent stop** — and every adapter keeps them identically
(SDS §3.9.5):

* :class:`FakeDrive` records an assertable trace and integrates an **odometer** from the
  commanded legs, so a behaviour test asserts *"the robot ended up where it started"* rather
  than mocking a call. It *is* the simulator: stdlib only, no motors, same contract.
* :class:`L9110sDrive` drives two N20 gear motors through one L9110S dual H-bridge on four
  GPIO pins via ``gpiozero``. That library is apt-shipped on Raspberry Pi OS and resolves
  through the venv's ``--system-site-packages`` (ADR-008, exactly like ``picamera2``), so it is
  imported **only** inside this module and **lazily**, inside the method that first needs it —
  the module imports cleanly on CI and a laptop, where the fake path and mypy still load it.

**The direction flip lives here, once.** Measured on the rig (2026-08-31, SDS §3.9.5):
``gpiozero.Motor(forward=IA, backward=IB).forward()`` on *both* channels drives the chassis
toward its own back. The wiring diagram assumed the mirrored motor mount would need asymmetric
per-wheel inversion; the hubs cancel it, so it is one global flag — ``[drive]
forward_is_inverted`` — applied at the moment a signed port speed becomes a motor command, and
nothing above this port ever sees a sign convention. A rig whose flip is wrong drives backward
on ``STEP_TOWARD`` and *walks off the far edge*, which is why the flag is a config value the
bench measures and not a constant the code guesses.

``odometer_mm`` and ``is_running`` are adapter-level introspection, deliberately **off** the
``Drive`` port — ``DriveService`` keeps its own odometer from the milliseconds :meth:`run`
returns and never reads the adapter's back, so they are not an application need (they are the
contract suite's observation points, exactly as ``FakeServo.position`` is). The two odometers
agreeing is what a service test asserts; the fake's is the one that would catch a service that
lied about what it commanded. Constructed only by the composition root or a test fixture (P3).
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from avid.core.hal import DriveCapabilities

# Update cadence for a run. ~20 ms/slice matches the servo adapters and the edge sensor's
# default poll: fine enough that a stop lands within one poll of the sensor that asked for
# it, and coarse enough that a 300 ms leg is ~15 awaited slices, each a chance to observe a
# cancel or a stop.
_STEP_MS = 20


def _clamp(speed: float) -> float:
    """Clamp a signed wheel speed to ``[-1, 1]`` — the adapter's job, like a servo's reach."""
    return max(-1.0, min(1.0, speed))


def _slices(duration_ms: int) -> int:
    """Number of awaited slices for a run of *duration_ms* (at least one)."""
    return max(1, duration_ms // _STEP_MS)


class FakeDrive:
    """The :class:`~avid.core.ports.Drive` fake (P6): a trace, an odometer, no motors.

    ``capabilities`` is injected (P7); ``None`` models a rig with no wheels — the port still
    answers, the planner plans nothing, and :meth:`run` records the call it should never
    receive. The public :attr:`runs` list is the assertable trace — one ``(left, right,
    driven_ms)`` entry per completed, stopped or cancelled run — and :attr:`odometer_mm`
    integrates ``mean(left, right) × mm_per_s_at_full`` over the slices that actually ran, so
    a stopped run advances it only as far as it got. :meth:`run` sleeps between slices, so a
    preempting cancel genuinely stops it partway and the trace is left partial.
    """

    def __init__(self, *, capabilities: DriveCapabilities | None) -> None:
        self._capabilities = capabilities
        self._running = False
        self._stop_requested = False
        # The assertable record of every run, in order: (left, right, ms actually driven).
        self.runs: list[tuple[float, float, int]] = []
        # Position along the robot's forward axis, mm, integrated from what was driven.
        self.odometer_mm = 0.0
        self.stops = 0

    @property
    def capabilities(self) -> DriveCapabilities | None:
        """What these wheels can do, for negotiation (SDS §3.9.3 / ADR-015)."""
        return self._capabilities

    async def run(self, left: float, right: float, *, duration_ms: int) -> int:
        """Drive at the clamped speeds for *duration_ms*, in awaited slices.

        Returns the milliseconds actually driven: the whole of *duration_ms* if the run
        completes, less if :meth:`stop` was called or the task was cancelled mid-run. The
        odometer advances slice by slice, so it reflects exactly what was driven. The trace
        entry is appended in ``finally`` so a cancelled run is recorded as partial rather than
        vanishing.
        """
        left, right = _clamp(left), _clamp(right)
        n = _slices(duration_ms)
        slice_ms = duration_ms / n
        driven_ms = 0.0
        self._running = True
        self._stop_requested = False
        try:
            for _ in range(n):
                if self._stop_requested:
                    break
                await asyncio.sleep(slice_ms / 1000)
                driven_ms += slice_ms
                if self._capabilities is not None:
                    self.odometer_mm += (
                        (left + right)
                        / 2
                        * self._capabilities.mm_per_s_at_full
                        * slice_ms
                        / 1000
                    )
        finally:
            self._running = False
            self.runs.append((left, right, round(driven_ms)))
        return round(driven_ms)

    async def stop(self) -> None:
        """Cut the motors now (SDS §9.1.4): a run in flight ends at its next slice and
        returns what it drove. Idempotent — counting is the only side effect when idle."""
        self.stops += 1
        self._stop_requested = True

    @property
    def is_running(self) -> bool:
        """Whether a run is in flight (introspection, off the port)."""
        return self._running


class L9110sDrive:
    """The real :class:`~avid.core.ports.Drive`: two N20 motors through an L9110S on GPIO (#400).

    Each channel is one ``gpiozero.Motor(forward=IA, backward=IB)``, which maps straight onto
    the L9110S truth table (``IA`` high + ``IB`` low = that channel's "forward"). Pins are BCM
    numbers from ``[drive]`` (P7); the shipped map is left ``5/6``, right ``12/13``, measured.

    **Threading.** ``gpiozero`` writes are syscalls into the ``lgpio`` pin factory — sub-
    millisecond, but blocking, so every motor write goes through ``asyncio.to_thread`` (P8) and
    holds :attr:`_device_lock` around the write, the same discipline ``Pca9685Servo`` keeps. The
    run itself sleeps on ``asyncio.sleep`` in short slices so a cancel or a :meth:`stop` lands
    within one slice, and the motors are cut in a ``finally`` — a wheel left turning is the one
    failure that outlives the program.

    ⚠️ **Under systemd, ``lgpio`` needs a writable working directory** (``LG_WD``) and the
    service user needs the ``gpio`` group — ``deploy/robot.service`` already carries both for
    the servos (#413), and this adapter inherits the trap: *works by hand, dead under the unit*
    is a permission, not a bug in here (``PI_OPERATIONS.md`` §5c.2 / §5d).

    ⚠️ **Power.** The motors share the 5–6 V rail with the servos, common ground only, never the
    Pi's 5 V pin (R-04). Two N20s stall at ~1 A together; ``[drive] speed_frac`` keeps a step at a
    wiring-check duty, and #206's brown-out measurement is owed again with four actuators.

    ``is_running`` is introspection, off the port, for the contract suite.
    """

    def __init__(
        self,
        *,
        capabilities: DriveCapabilities,
        left_forward_pin: int,
        left_backward_pin: int,
        right_forward_pin: int,
        right_backward_pin: int,
        forward_is_inverted: bool,
    ) -> None:
        self._capabilities = capabilities
        self._pins = (
            (left_forward_pin, left_backward_pin),
            (right_forward_pin, right_backward_pin),
        )
        self._inverted = forward_is_inverted
        # (left, right) gpiozero.Motor objects, created on first use — the import and the pin
        # claim both happen lazily, so constructing this adapter on a laptop is harmless.
        self._motors: tuple[Any, Any] | None = None
        self._device_lock = threading.Lock()
        self._running = False
        self._stop_requested = False

    @property
    def capabilities(self) -> DriveCapabilities:
        """The measured (or provisional — SPK-6) speed of these wheels (SDS §3.9.3)."""
        return self._capabilities

    async def run(self, left: float, right: float, *, duration_ms: int) -> int:
        """Drive at the clamped robot-frame speeds for *duration_ms*, cancellable, stoppable.

        Returns the milliseconds actually driven — the whole of *duration_ms* if the run ran
        out, less if :meth:`stop` was called or the task was cancelled mid-run. The motors are
        cut in ``finally`` on every path.
        """
        left, right = _clamp(left), _clamp(right)
        n = _slices(duration_ms)
        slice_ms = duration_ms / n
        driven_ms = 0.0
        self._running = True
        self._stop_requested = False
        try:
            await asyncio.to_thread(self._set_blocking, left, right)
            for _ in range(n):
                if self._stop_requested:
                    break
                await asyncio.sleep(slice_ms / 1000)
                driven_ms += slice_ms
        finally:
            self._running = False
            await asyncio.to_thread(self._stop_blocking)
        return round(driven_ms)

    async def stop(self) -> None:
        """Cut both motors now. Idempotent. A run in flight ends at its next slice."""
        self._stop_requested = True
        await asyncio.to_thread(self._stop_blocking)

    @property
    def is_running(self) -> bool:
        """Whether a run is in flight (introspection, off the port)."""
        return self._running

    def close(self) -> None:
        """Release the pins. Stops the motors first — closing a driven pin leaves it floating,
        and a floating L9110S input is not guaranteed to read as "off"."""
        with self._device_lock:
            if self._motors is None:
                return
            for motor in self._motors:
                motor.stop()
                motor.close()
            self._motors = None

    # --- worker-thread helpers: blocking device I/O, one critical section each -------------

    def _motors_locked(self) -> tuple[Any, Any]:
        """The two ``Motor`` objects, created on first use. Caller holds :attr:`_device_lock`."""
        if self._motors is None:
            # Lazy, Pi-only: apt's python3-gpiozero via --system-site-packages (ADR-008). On the
            # rig's Bookworm image gpiozero's default pin factory is lgpio, which is why LG_WD
            # matters under systemd (#413).
            from gpiozero import Motor

            left, right = (
                Motor(forward=forward, backward=backward)
                for forward, backward in self._pins
            )
            self._motors = (left, right)
        assert self._motors is not None
        return self._motors

    def _set_blocking(self, left: float, right: float) -> None:
        """Apply two signed robot-frame speeds to the two channels — the flip applied HERE."""
        with self._device_lock:
            for motor, speed in zip(self._motors_locked(), (left, right), strict=True):
                # + at the port is "toward the user". On this rig Motor.forward() drives the
                # chassis backward, so the port's + becomes the motor's backward when inverted.
                toward = speed > 0
                magnitude = abs(speed)
                if magnitude == 0:
                    motor.stop()
                elif toward != self._inverted:
                    motor.forward(magnitude)
                else:
                    motor.backward(magnitude)

    def _stop_blocking(self) -> None:
        with self._device_lock:
            if self._motors is None:
                return  # never energised — nothing to cut, and no reason to claim the pins now
            for motor in self._motors:
                motor.stop()
