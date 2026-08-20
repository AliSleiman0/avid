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
import threading
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

    **Concurrency (#289, the AVID-266 shape).** Every hardware touch happens on a
    ``to_thread`` worker, so this adapter owns its own serialisation — the caller cannot
    provide it, and until M9 there was no caller that needed to. One
    :class:`threading.Lock` guards two things that were both check-then-act:

    * The **lazy ``ServoKit`` construction**. Two workers could each find ``self._kit is
      None`` and each build one, which on a PCA9685 means two objects re-running
      ``set_pulse_width_range`` and ``actuation_range`` against the same I2C address —
      the servo equivalent of AVID-266's leaked framebuffer fds, and quiet in exactly
      the same way, because the last one built works fine.
    * The **write and the bookkeeping that records it**, held as one critical section
      rather than a lock around each. Splitting them is what makes :meth:`position` lie:
      worker A writes 50°, worker B writes 20°, then A records 50 — and the adapter now
      reports an angle the horn is not at. Nothing reads it back to notice (a servo has
      no feedback), so the disagreement survives until someone looks at the robot.

    ⚠️ **What the lock does not buy, and the caller must still keep.** It makes each
    operation atomic; it does not order them. A :meth:`relax` racing a live sweep can
    still be followed by that sweep's next step re-energising the channel — the servo
    ends up held, silently, which is precisely the buzz the M9 gate listens for. The
    invariant is therefore **one operation per channel in flight at a time**, and
    ``MotionService`` keeps it by *cancelling* the gesture before relaxing rather than
    relaxing underneath it. Stated here because #203's preemption is the first code in
    the project that can break it. (``AlsaSpeaker`` carries the same kind of paragraph
    for the same kind of reason.)

    Latent until M9 and filed rather than observed: through M2–M8 the only caller was the
    bring-up demo, which awaited one move at a time. #203 wires preemption, which *is* a
    second ``move_to`` arriving while the first is still on a worker thread.

    ⚠️ **``actuation_deg`` is the servo's electrical span; an axis's ``max_deg`` is the
    linkage's safe reach** (#356). ``ServoKit``'s ``actuation_range`` calibrates the
    degree→pulse mapping: it says what angle the full ``min_pulse_us``–``max_pulse_us``
    range sweeps, which is a property of the **part** (~180° for an SG90/MG90S). This
    adapter used to set it from ``axis.max_deg``, which is a property of the **mounting**
    — true only while the two happen to be equal, as they were at ``180`` on the M2 rig.
    Narrow a reach to keep the head out of its own chassis and every commanded angle on
    this adapter is then off by ``actuation_deg / max_deg``, while :meth:`position`, the
    contract suite and ``FakeServo`` all keep agreeing that nothing is wrong — because
    none of them can see a pulse width. Only the horn can. Two names, two meanings, and
    the clamp keeps using ``max_deg`` exactly as SDS §3.9.1 says it should.
    """

    def __init__(
        self,
        *,
        axes: tuple[Axis, ...],
        i2c_address: int,
        min_pulse_us: int,
        max_pulse_us: int,
        freq_hz: int,
        actuation_deg: float = 180.0,
    ) -> None:
        self._axes = axes
        self._by_channel = {axis.channel: axis for axis in axes}
        self._i2c_address = i2c_address
        self._min_pulse_us = min_pulse_us
        self._max_pulse_us = max_pulse_us
        self._freq_hz = freq_hz
        self._actuation_deg = actuation_deg
        # The ServoKit handle, built on first use. Untyped (Any) because the library ships
        # no stubs (mypy resolves it via ignore_missing_imports).
        self._kit: Any | None = None
        self._position: dict[int, float] = {axis.channel: axis.min_deg for axis in axes}
        self._energised: dict[int, bool] = {axis.channel: False for axis in axes}
        # Guards the lazy kit build and every write-plus-bookkeeping pair (#289). A
        # threading.Lock, not an asyncio one: it is only ever taken on the worker thread
        # inside asyncio.to_thread, never on the event loop.
        self._device_lock = threading.Lock()

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

        ``start`` is read outside the lock on purpose: it is this sweep's origin, sampled
        once, and a concurrent write moving it afterwards is the caller's ordering problem
        (see the class docstring), not a torn read.
        """
        target = _clamp(angle_deg, self._by_channel[channel])
        start = self._position[channel]
        n = _steps(duration_ms)
        step_dt = duration_ms / n / 1000
        for i in range(1, n + 1):
            await asyncio.sleep(step_dt)
            angle = start + (target - start) * i / n
            await asyncio.to_thread(self._write_blocking, channel, angle)

    async def relax(self, channel: int) -> None:
        """Cut the pulse on *channel* (``angle = None``) so the servo de-energizes (no buzz)."""
        await asyncio.to_thread(self._relax_blocking, channel)

    def _kit_locked(self) -> Any:
        """Build the ``ServoKit`` on first use. Caller holds :attr:`_device_lock` (#289).

        Lazy, Pi-only import — kept out of module scope so this file loads off-Pi
        (P5, ADR-008). The library has no stubs; mypy resolves it via override.
        """
        if self._kit is None:
            from adafruit_servokit import ServoKit

            kit = ServoKit(channels=16, address=self._i2c_address)
            for axis in self._axes:
                servo = kit.servo[axis.channel]
                servo.set_pulse_width_range(self._min_pulse_us, self._max_pulse_us)
                # The part's span, never the axis's reach — see the ⚠️ in the class
                # docstring (#356). Clamping to the reach is _clamp's job, and stays so.
                servo.actuation_range = self._actuation_deg
            self._kit = kit
        return self._kit

    def _write_blocking(self, channel: int, angle: float) -> None:
        """Write one angle and record it as one critical section (#289).

        The record has to be inside the lock with the write it describes: two workers
        writing 50° then 20° and recording in the other order leave :meth:`position`
        reporting an angle the horn is not at, and nothing ever reads a servo back to
        notice.
        """
        with self._device_lock:
            self._kit_locked().servo[channel].angle = angle
            self._position[channel] = angle
            self._energised[channel] = True

    def _relax_blocking(self, channel: int) -> None:
        # ServoKit de-energizes a channel when its angle is set to None (no pulse).
        with self._device_lock:
            self._kit_locked().servo[channel].angle = None
            self._energised[channel] = False

    def position(self, channel: int) -> float:
        """Last commanded angle for *channel* (a servo has no feedback; off the port)."""
        return self._position[channel]

    def is_energised(self, channel: int) -> bool:
        """Whether *channel*'s last command left a pulse on it (off the port)."""
        return self._energised[channel]
