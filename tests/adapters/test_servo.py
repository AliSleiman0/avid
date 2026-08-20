"""Adapter tests for :class:`~avid.adapters.servo.Pca9685Servo` (#289, #356).

The port's *behaviour* — clamp, cancel, relax — is proven for both adapters by the contract
suite (``tests/contract/test_servo.py``). What cannot live there is what this file holds: two
defects the **fake cannot have**, because one needs two operations in flight against a real
device and the other needs a pulse width. Both are therefore invisible to every test that
treats the adapters as interchangeable, which is most of them, and rightly so.

The shape is AVID-266's, found by auditing every adapter for it after the framebuffer paid for
it once. ``Pca9685Servo`` offloads every hardware touch to ``asyncio.to_thread``, and both the
lazy ``ServoKit`` build and the position bookkeeping were check-then-act across that hop. It was
**latent** rather than observed: through M2–M8 the only caller was the bring-up demo, awaiting one
move at a time. #203 wires gesture preemption, which is precisely a second ``move_to`` arriving
while the first is still on a worker thread — so it is fixed before the caller that reaches it
exists, not after.

⚠️ **These tests widen the window on purpose, and that is what makes them tests rather than
lotteries.** AVID-266 measured the alternative: with its lock removed, two concurrent renders
never lost the race, eight never did either, and it took *thirty-two* before the defect appeared —
a test that would quietly stop proving anything on a slower box. The widening here is a
**bounded rendezvous**: the first worker inside the critical section waits for the second to reach
it, with a timeout. Without the lock the second arrives and the interleaving is deterministic;
with the lock the second cannot arrive, the wait times out, and the test passes without hanging.
The race is real; only its width is ours.

``adafruit_servokit`` is Pi-only (ADR-008) and absent here, so these drive the real adapter against
a stub module installed in ``sys.modules`` — the same lazy import the adapter already relies on for
loading off-Pi. A hand-written stub, not ``unittest.mock``: SDS §14.3 permits mocks in this
directory and the stub is still better, because it has to *behave* like a servo channel (hold an
angle, accept ``None``) rather than merely record that it was called.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types

import pytest

from avid.adapters.servo import Pca9685Servo
from avid.core.hal import Axis

_PAN = Axis(name="pan", channel=0, min_deg=0.0, max_deg=180.0)
_TILT = Axis(name="tilt", channel=13, min_deg=0.0, max_deg=180.0)

# How long the first worker holds the window open waiting for the second. Only ever paid in
# full when the lock is doing its job (the second worker cannot arrive), so it is a one-off
# cost on the passing path, not a per-test sleep.
_RENDEZVOUS_S = 0.25


class _Rendezvous:
    """Holds the first arrival until the second arrives, or until *timeout_s* elapses.

    This is the widened window. It is deliberately **not** a :class:`threading.Barrier`: a
    barrier deadlocks the fixed adapter, because the second worker is blocked on the very lock
    the first is holding and can never reach it. A timeout turns "the second could not get in" —
    which is the property under test — into a bounded wait and a pass.
    """

    def __init__(self, *, timeout_s: float = _RENDEZVOUS_S) -> None:
        self._timeout_s = timeout_s
        self._second_arrived = threading.Event()
        self._count = 0
        self._count_lock = threading.Lock()

    def arrive(self) -> None:
        with self._count_lock:
            self._count += 1
            first = self._count == 1
        if first:
            self._second_arrived.wait(self._timeout_s)
        else:
            self._second_arrived.set()

    @property
    def arrivals(self) -> int:
        with self._count_lock:
            return self._count


class _StubChannel:
    """One PCA9685 channel: holds the last commanded angle, ``None`` meaning de-energised."""

    def __init__(self) -> None:
        self.angle_value: float | None = None
        self.pulse_range: tuple[int, int] | None = None
        self.actuation_range: float | None = None
        # Armed by a test *after* any warm-up write, so the rendezvous counts only the pair
        # under test. Arming it at construction would spend the "first arrival" on the warm-up
        # and leave the window closed for the race — a test that passes either way.
        self.on_write: _Rendezvous | None = None

    # ``angle`` is a property so a write can be observed *after* the value lands, which is where
    # the defect's window is: the adapter's hardware write and its bookkeeping were two steps,
    # and the interleaving that matters happens between them.
    @property
    def angle(self) -> float | None:
        return self.angle_value

    @angle.setter
    def angle(self, value: float | None) -> None:
        self.angle_value = value
        if self.on_write is not None:
            self.on_write.arrive()

    def set_pulse_width_range(self, min_us: int, max_us: int) -> None:
        self.pulse_range = (min_us, max_us)


class _StubKit:
    """Stands in for ``adafruit_servokit.ServoKit``, counting how often it is constructed.

    One object plays both the class and the instance: calling it is the construction the
    adapter performs, and it returns itself so ``kit.servo[channel]`` reaches the same
    channels every time. That is what lets ``builds`` be a count rather than a guess.
    """

    def __init__(self, *channels: int) -> None:
        self.builds: list[int] = []
        self.servo: dict[int, _StubChannel] = {c: _StubChannel() for c in channels}
        self.on_build: _Rendezvous | None = None

    def __call__(self, *, channels: int, address: int) -> _StubKit:
        self.builds.append(address)
        if self.on_build is not None:
            self.on_build.arrive()
        return self


def _install(monkeypatch: pytest.MonkeyPatch, kit: _StubKit) -> None:
    """Put the stub where the adapter's lazy ``from adafruit_servokit import ServoKit`` finds it."""
    module = types.ModuleType("adafruit_servokit")
    module.ServoKit = kit  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "adafruit_servokit", module)


def _servo(*axes: Axis, actuation_deg: float = 180.0) -> Pca9685Servo:
    return Pca9685Servo(
        axes=axes,
        i2c_address=0x40,
        min_pulse_us=500,
        max_pulse_us=2500,
        freq_hz=50,
        actuation_deg=actuation_deg,
    )


async def test_the_kit_is_built_once_under_concurrent_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#289 AC-1, first half: the lazy build is a check-then-act across a thread hop.

    Two workers could each find ``self._kit is None`` and each construct a ``ServoKit``, which on
    real hardware means two objects re-running ``set_pulse_width_range`` and ``actuation_range``
    against the same I²C address. Quiet in exactly the way AVID-266's leaked framebuffer fds were
    quiet: the last one built works fine, so nothing looks wrong.

    The stub's constructor is the widened window — the first build waits inside it for a second to
    arrive. With the lock, the second worker is still queued outside and never does."""
    kit = _StubKit(0, 13)
    kit.on_build = _Rendezvous()
    _install(monkeypatch, kit)
    servo = _servo(_PAN, _TILT)

    await asyncio.gather(
        servo.move_to(0, 45.0, duration_ms=1),
        servo.move_to(13, 45.0, duration_ms=1),
    )

    assert kit.builds == [0x40], (
        f"the PCA9685 was constructed {len(kit.builds)} times — the lazy build raced"
    )


async def test_relax_builds_the_kit_at_most_once_alongside_a_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same check-then-act reached through the *other* entry point.

    ``relax`` offloads to a worker exactly as ``move_to`` does, so a first-ever relax racing a
    first-ever move is the same double build. Worth its own case because #203's fault path relaxes
    every channel while a sweep may still be in flight — the two methods meeting is not
    hypothetical, it is the I²C-abort path (SDS §3.12.3)."""
    kit = _StubKit(0, 13)
    kit.on_build = _Rendezvous()
    _install(monkeypatch, kit)
    servo = _servo(_PAN, _TILT)

    await asyncio.gather(servo.move_to(0, 30.0, duration_ms=1), servo.relax(13))

    assert kit.builds == [0x40]


async def test_position_reports_the_angle_that_was_actually_written_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#289 AC-1, second half: the write and the record it describes are one critical section.

    Splitting them is what makes :meth:`position` lie. Worker A writes 50°, worker B writes 20°,
    then A records 50 — and the adapter reports an angle the horn is not at. Nothing ever reads a
    servo back (there is no feedback), so the disagreement is invisible until someone looks at the
    robot, which is the M9 gate's whole reason for existing.

    The rendezvous sits *after* the stub stores the value, so it holds open the gap between the
    hardware write and the bookkeeping — the exact gap the defect lives in. ``duration_ms=1``
    makes each move a single step (``_steps`` floors to one), so this races two writes rather than
    two sweeps."""
    kit = _StubKit(0)
    _install(monkeypatch, kit)
    servo = _servo(_PAN)
    # Build the kit first, so the pair below races on the *write*. Without this they race in the
    # lazy build instead and this test is a second copy of the one above. Arming the rendezvous
    # afterwards is part of the same point: a warm-up write would otherwise spend the first
    # arrival and leave the window shut.
    await servo.move_to(0, 10.0, duration_ms=1)
    kit.servo[0].on_write = write = _Rendezvous()

    await asyncio.gather(
        servo.move_to(0, 50.0, duration_ms=1),
        servo.move_to(0, 20.0, duration_ms=1),
    )

    assert write.arrivals == 2, (
        "the widened window was never reached — the test proves nothing"
    )
    assert servo.position(0) == kit.servo[0].angle_value, (
        "position() reports an angle the channel was not left at"
    )


async def test_a_relaxed_channel_is_not_reported_energised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same invariant for the flag the gate's "no buzz" clause is graded on.

    ``is_energised`` and the pulse on the wire must agree for the same reason ``position`` and the
    horn must: a channel reported relaxed while it is still held is a robot that hums on a desk all
    night, and the only instrument that disagrees is an ear in a quiet room.

    ⚠️ This asserts *atomicity*, not ordering. A relax racing a live sweep can still be followed by
    that sweep's next step re-energising the channel — the lock cannot fix that and does not claim
    to. ``MotionService`` keeps the ordering invariant by cancelling a gesture before relaxing it
    (#203); the adapter's class docstring says so in as many words."""
    kit = _StubKit(0)
    _install(monkeypatch, kit)
    servo = _servo(_PAN)
    await servo.move_to(0, 10.0, duration_ms=1)
    kit.servo[0].on_write = write = _Rendezvous()

    await asyncio.gather(servo.move_to(0, 60.0, duration_ms=1), servo.relax(0))

    assert write.arrivals == 2
    energised_on_the_wire = kit.servo[0].angle_value is not None
    assert servo.is_energised(0) is energised_on_the_wire, (
        "is_energised disagrees with whether the channel is carrying a pulse"
    )


def test_the_caller_invariant_is_recorded_on_the_class() -> None:
    """AC-2: this codebase expects the *why* written down, not just the lock.

    ``AlsaSpeaker`` carries the same kind of paragraph — an invariant the adapter cannot enforce
    and the caller must keep. Asserted rather than trusted because a docstring is the only place
    that invariant can live, and a future tidy-up that drops it removes the only warning
    ``MotionService`` gets before it breaks it."""
    doc = Pca9685Servo.__doc__ or ""
    assert "one operation per channel in flight at a time" in doc
    assert "#289" in doc


# --- the degree→pulse calibration (#356) --------------------------------------


async def test_actuation_range_is_the_servos_span_not_the_axiss_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#356: the two quantities are different and were conflated.

    ``ServoKit.actuation_range`` says what angle the full ``min_pulse_us``–``max_pulse_us``
    range sweeps — a property of the **part**. ``Axis.max_deg`` is the reach of the **mounting**,
    the limit ``_clamp`` enforces (SDS §3.9.1). This adapter derived the first from the second,
    which is true only while they happen to be equal — as they were at ``180`` on the whole M2
    rig, which is why it survived a hardware bring-up.

    The axis here is deliberately narrower than the servo, the way #200's provisional reaches
    will be: a tilt linkage that must not drive the head into its own chassis. Under the old
    code ``actuation_range`` becomes ``120``, and the pulse for a commanded 60° then means 90°
    of real travel — off by half — while ``position()`` reports 60, the trace looks perfect and
    ``FakeServo`` (which ignores pulse widths entirely) agrees. The only instrument that
    disagrees is the horn, which is the M4 lesson wearing a servo."""
    narrow = Axis(name="tilt", channel=13, min_deg=30.0, max_deg=120.0)
    kit = _StubKit(13)
    _install(monkeypatch, kit)
    servo = _servo(narrow, actuation_deg=180.0)

    await servo.move_to(13, 60.0, duration_ms=1)

    assert kit.servo[13].actuation_range == 180.0, (
        "actuation_range was calibrated from the linkage's reach, not the servo's span"
    )
    # And the reach still governs what may be commanded — the clamp is untouched.
    assert kit.servo[13].pulse_range == (500, 2500)
    await servo.move_to(13, 400.0, duration_ms=1)
    assert servo.position(13) == 120.0
