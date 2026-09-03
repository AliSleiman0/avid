"""The step vocabulary, the pure step planner, and the three ``drive.*`` events (#400, ADR-015).

The pure half of M12, shaped exactly like :mod:`avid.domain.motion` is for M9 — and for the
same reason. SDS §3.9.5 admits *"centimetre-scale, net-zero steps on the desk"* and nothing
more, and the way to make *net-zero* and *bounded* claims a test can fail rather than hopes a
bench confirms is to make them properties of a **value**: a plan. So :func:`plan_step` turns a
step gesture into a tuple of :class:`Leg` values with no I/O, no clock, no randomness and no
motor, and two one-line functions — :func:`net_mm` and :func:`peak_excursion_mm` — say what any
plan would do to the robot's position before a wheel turns.

**Net-zero is by construction, not by promise.** Every step plan is *out-leg → dwell →
return-leg* with the two moving legs of equal magnitude and opposite sign, emitted in the
**same tuple**. A service cannot perform the out-leg and forget the return; it performs a plan,
and the plan already contains its own undoing. That is what lets ``tests/domain/test_drive.py``
assert that a randomly ordered sequence of plans sums to zero — which is the claim *"cannot
accumulate error over a thirty-day soak"* actually rests on.

**Bounded is a clamp inside the planner, not a trust in the caller.** The excursion of a plan is
one leg, and the leg is clamped to ``max_excursion_mm`` here even though the config validator
already refuses a longer ``step_mm``: a planner that leaned on config validation would produce
a robot that walks past its budget the day someone constructs the geometry by hand.

**The direction convention is stated once, here.** :attr:`Heading.FORWARD` is *toward the
user* — the front of the robot, where the edge sensors are. At the :class:`~avid.core.ports.Drive`
port ``+`` means forward on every rig; the measured fact that ``Motor.forward()`` on this
particular H-bridge drives the chassis *backward* is the adapter's to absorb, once, from
``[drive] forward_is_inverted``. Nothing in this module knows a sign convention exists. (It is
called ``Heading`` and not ``Direction`` because :class:`avid.domain.motion.Direction` already
names where the model may ask the head to *look* — a different vocabulary with a different
owner, and one enum serving both would let a ``look_at`` argument become a wheel command.)

**Why ``DriveCapabilities`` is declared here rather than in ``core/hal.py``.** The same P1
reason as ``Axis`` and ``BBox``: ``drive.*`` events live in the domain, the planner needs the
one number the rig reports (``mm_per_s_at_full``), and the ``layers`` contract puts ``core``
above ``domain``. ``core/hal.py`` re-exports it, so ``avid.core.hal.DriveCapabilities`` stays
the spelling every port and adapter uses (SDS §3.9.4's note, applied a third time).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from typing import ClassVar

from avid.domain.events import Event
from avid.domain.motion import Gesture

# The four documented abort reasons (SDS §9.1.3, the `drive.step_aborted` row). Module
# constants so the service, Observability and the tests agree on the spelling, and so a
# reader can grep for the one that matters — EDGE is the row that means the sensors earned
# their place.
EDGE = "edge"
FAULT = "fault"
PREEMPTED = "preempted"
BUDGET = "budget"
ABORT_REASONS: tuple[str, ...] = (EDGE, FAULT, PREEMPTED, BUDGET)

# The two gestures this module realises. Everything else in ``Gesture`` is a servo intent and
# plans to an empty tuple here, exactly as these two plan to an empty tuple in ``motion.plan``.
STEP_GESTURES: frozenset[Gesture] = frozenset({Gesture.STEP_TOWARD, Gesture.STEP_BACK})


class Heading(Enum):
    """Which way a leg moves the robot, in the **robot's** frame (SDS §3.9.5).

    ``FORWARD`` is toward the user and toward the edge sensors. A small closed set on purpose:
    the SDS admits straight steps and nothing else, and a heading enum with no ``LEFT`` is what
    keeps a pivot from being planned by accident.
    """

    FORWARD = auto()
    BACKWARD = auto()


@dataclass(frozen=True, slots=True, kw_only=True)
class DriveCapabilities:
    """What the wheels can do — for capability negotiation (SDS §3.9.3, §3.9.5).

    Read via :attr:`~avid.core.ports.Drive.capabilities`. ``None`` at the port means *this rig
    has no wheels*, and :func:`plan_step` answers every step gesture with an empty plan.

    ``mm_per_s_at_full`` is the one number the excursion budget is only as good as: the planner
    turns a distance into a duration through it, and the service's odometer turns durations back
    into distances. It is a **measured property of this motor-and-wheel pair** (PMP SPK-6), which
    is why it rides the adapter's report rather than living here as a constant.
    """

    mm_per_s_at_full: float


@dataclass(frozen=True, slots=True, kw_only=True)
class StepGeometry:
    """The shape of a step, as ``[drive]`` declares it (SDS §9.6) — bundled so a plan has one
    input for *what a step is* beside one for *what the rig can do*.

    Values, not policy: how far, how fast, how long to pause, and the budget. A service owns
    *when*; the geometry owns *what*. Validated at config load (``DriveConfig``), and clamped
    again by the planner because a pure function does not get to assume its inputs were.
    """

    step_mm: float
    max_excursion_mm: float
    speed_frac: float
    dwell_ms: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Leg:
    """One piece of a step: drive ``heading`` at ``speed_frac`` for ``duration_ms``, or dwell.

    ``heading is None`` is a dwell — the pause at the far end of a step. It has a duration and
    no motion, so the service can treat every leg identically (wait it out, watch the sensors)
    and a plan's total time is the plain sum of its legs.

    ``distance_mm`` is **signed in the robot frame** (``+`` forward) and is the planner's own
    arithmetic, recorded on the leg so :func:`net_mm` and :func:`peak_excursion_mm` are sums
    rather than re-derivations — two places computing distance from duration is how they drift.
    """

    heading: Heading | None
    speed_frac: float
    duration_ms: int
    distance_mm: float

    @property
    def wheels(self) -> tuple[float, float]:
        """The ``(left, right)`` signed speeds :meth:`~avid.core.ports.Drive.run` takes.

        A straight step drives both wheels identically; a dwell drives neither. The sign is the
        heading, and ``+`` is forward at the port on every rig (SDS §3.9.5).
        """
        if self.heading is None:
            return (0.0, 0.0)
        signed = (
            self.speed_frac if self.heading is Heading.FORWARD else -self.speed_frac
        )
        return (signed, signed)


def _leg_ms(distance_mm: float, speed_frac: float, caps: DriveCapabilities) -> int:
    """How long *distance_mm* takes at *speed_frac* of full — never zero for a real distance.

    Rounded to the nearest millisecond, floored at 1: a leg of zero duration is a jump, and the
    adapter would turn it into a division by zero when it computes its step count (the same
    argument :mod:`avid.domain.motion` makes for keyframes).
    """
    mm_per_s = speed_frac * caps.mm_per_s_at_full
    return max(1, round(distance_mm / mm_per_s * 1000.0))


def plan_step(
    gesture: Gesture, caps: DriveCapabilities | None, geometry: StepGeometry
) -> tuple[Leg, ...]:
    """The legs *gesture* becomes on a rig with *caps*. Pure (SDS §3.9.3, §3.9.5).

    Same inputs, same output: no clock, no randomness, no globals, no I/O. Three answers:

    * a step gesture on a rig with wheels → ``(out, dwell, back)``, with ``out`` and ``back`` of
      equal length and opposite heading, so the plan is **net-zero by construction**;
    * a step gesture on a rig with ``caps is None`` → ``()`` — *"this rig has no wheels"* is a
      legitimate answer, not an exception, and the service treats it as a no-op (the shape
      §3.9.4 established for a 1-servo rig asked to look up);
    * any other gesture → ``()`` — a nod is not a step, and this planner does not pretend.

    The leg length is ``min(step_mm, max_excursion_mm)``: **bounded inside the planner**, so the
    excursion of any plan this function returns is at most the budget, whatever the caller
    thought it was asking for. A dwell of ``0`` ms is omitted rather than emitted as an empty
    leg, so a plan never carries a member that does nothing.
    """
    if caps is None or gesture not in STEP_GESTURES:
        return ()
    leg_mm = min(geometry.step_mm, geometry.max_excursion_mm)
    out = Heading.FORWARD if gesture is Gesture.STEP_TOWARD else Heading.BACKWARD
    back = Heading.BACKWARD if out is Heading.FORWARD else Heading.FORWARD
    duration = _leg_ms(leg_mm, geometry.speed_frac, caps)
    legs = [
        Leg(
            heading=out,
            speed_frac=geometry.speed_frac,
            duration_ms=duration,
            distance_mm=_signed(leg_mm, out),
        )
    ]
    if geometry.dwell_ms > 0:
        legs.append(
            Leg(
                heading=None,
                speed_frac=0.0,
                duration_ms=geometry.dwell_ms,
                distance_mm=0.0,
            )
        )
    legs.append(
        Leg(
            heading=back,
            speed_frac=geometry.speed_frac,
            duration_ms=duration,
            distance_mm=_signed(leg_mm, back),
        )
    )
    return tuple(legs)


def homing_leg(
    offset_mm: float, caps: DriveCapabilities, geometry: StepGeometry
) -> tuple[Leg, ...]:
    """The single leg that returns a robot sitting *offset_mm* from origin. Pure.

    A step that was cut short — by an edge, a preemption, a fault — leaves the robot off its
    origin by the part of the leg it did not run. The service keeps that shortfall as an
    offset and **homes first** on its next step (SDS §3.9.5), so an abort is a delay, not a
    drift. ``offset_mm`` is signed in the robot frame; the leg heads the opposite way and is
    clamped to the budget, because an offset larger than the budget is itself the thing the
    budget says cannot happen, and the honest response is to move at most one budget's worth
    and let the odometer say what is left.

    Returns ``()`` for an offset of zero — nothing to undo.
    """
    if offset_mm == 0.0:
        return ()
    distance = min(abs(offset_mm), geometry.max_excursion_mm)
    heading = Heading.BACKWARD if offset_mm > 0 else Heading.FORWARD
    return (
        Leg(
            heading=heading,
            speed_frac=geometry.speed_frac,
            duration_ms=_leg_ms(distance, geometry.speed_frac, caps),
            distance_mm=_signed(distance, heading),
        ),
    )


def _signed(distance_mm: float, heading: Heading) -> float:
    return distance_mm if heading is Heading.FORWARD else -distance_mm


def net_mm(legs: Iterable[Leg]) -> float:
    """Where a plan leaves the robot relative to where it started — ``0.0`` for every step plan.

    One function rather than a sum at every call site, for the reason :func:`avid.domain.motion.duration_ms`
    exists: the service's odometer, the ``drive.step_completed`` payload and the tests all need
    this number, and three derivations of it would be three chances to disagree.
    """
    return sum(leg.distance_mm for leg in legs)


def peak_excursion_mm(legs: Sequence[Leg]) -> float:
    """The furthest a plan takes the robot from its origin at any point while running it.

    The quantity the budget bounds — not the plan's *net* displacement, which is zero by
    construction and says nothing about how far out the robot went to get back to zero. Walks
    the cumulative position leg by leg and returns the largest absolute value it reaches.
    """
    position = 0.0
    peak = 0.0
    for leg in legs:
        position += leg.distance_mm
        peak = max(peak, abs(position))
    return peak


def step_duration_ms(legs: Iterable[Leg]) -> int:
    """How long a plan takes, in milliseconds — knowable **before** the wheels turn.

    Named ``step_duration_ms`` rather than ``duration_ms`` so it can sit beside
    :func:`avid.domain.motion.duration_ms` in ``avid.domain`` without one shadowing the other.
    """
    return sum(leg.duration_ms for leg in legs)


# --- the three drive.* events (SDS §9.1.3, transcribed) --------------------------------------
#
# Transcription, not design: §9.1.3 specifies publisher, subscribers, payload and overflow
# policy for all three. A separate family from ``motion.*`` on purpose (ADR-015): a step has a
# distance and an abort has a *reason*, where a gesture has axes, and ``motion.gesture_started``
# carrying an empty ``axes`` tuple for a step would be a row that lies by omission.
#
# These are observability. Nothing publishes "please step": the idle loop decides, the service
# performs, and ``Drive.run()`` is a direct awaited port call because losing it would be a
# correctness bug (§9.1.4) — and losing a ``Drive.stop()`` would be a robot on the floor.


@dataclass(frozen=True, slots=True, kw_only=True)
class DriveStepStarted(Event):
    """A step began moving the robot (SDS §9.1.3). Published by ``DriveService``.

    ``heading`` is the out-leg's direction as a string (``"forward"`` / ``"backward"``) and
    ``distance_mm`` the length of that leg — the two things a log line needs to say what the
    robot is about to do, without a reader re-deriving them from a plan.
    """

    name: ClassVar[str] = "drive.step_started"

    gesture: str
    heading: str
    distance_mm: float


@dataclass(frozen=True, slots=True, kw_only=True)
class DriveStepCompleted(Event):
    """A step ran to completion — out, dwell and back (SDS §9.1.3). Published by ``DriveService``.

    ``duration_ms`` is **measured** with ``monotonic_ns`` (§9.1.1), never planned; a measured
    duration that drifts from :func:`step_duration_ms` is a loop under load. ``net_mm`` is the
    plan's own arithmetic and is ``0.0`` for every plan this module emits — it rides the event
    so that a completed step whose payload says anything else is visibly a defect.
    """

    name: ClassVar[str] = "drive.step_completed"

    gesture: str
    duration_ms: int
    net_mm: float


@dataclass(frozen=True, slots=True, kw_only=True)
class DriveStepAborted(Event):
    """A step stopped before its plan finished (SDS §9.1.3). Published by ``DriveService``.

    ``reason`` is one of :data:`ABORT_REASONS`, and unlike ``motion.gesture_preempted``'s
    ``by`` it is a closed vocabulary rather than a name-or-``None``, because the four causes
    want four different responses: ``edge`` is the sensors earning their place (Observability
    logs it at WARNING), ``fault`` is the rig failing, ``preempted`` is ordinary, and
    ``budget`` is the odometer refusing to go further — which should never fire, and is a row
    precisely so that it is loud when it does.
    """

    name: ClassVar[str] = "drive.step_aborted"

    gesture: str
    reason: str


__all__ = [
    "ABORT_REASONS",
    "BUDGET",
    "EDGE",
    "FAULT",
    "PREEMPTED",
    "STEP_GESTURES",
    "DriveCapabilities",
    "DriveStepAborted",
    "DriveStepCompleted",
    "DriveStepStarted",
    "Heading",
    "Leg",
    "StepGeometry",
    "homing_leg",
    "net_mm",
    "peak_excursion_mm",
    "plan_step",
    "step_duration_ms",
]
