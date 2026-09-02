"""``Axis``, the gesture vocabulary, the pure planner, and the three ``motion.*`` events (#201).

The pure half of M9. What a gesture *is*, and what it looks like on whatever rig is actually
attached — with no I/O, no asyncio, no servo, and no clock. SDS §3.9.3 states the split this
module exists to build:

    The *gesture* stays in the domain layer as an intent; the *realization* is negotiated by
    the adapter's capabilities.

So :class:`Gesture` is an intent, and turning ``NOD`` into *"tilt to 78° over 220 ms, back to
90°, twice"* is a **pure function of the gesture and the axes the rig reports**. Everything
interesting about this milestone falls out of that: that a nod is a real tilt on the 2-servo rig
and degrades to a small pan wiggle on a 1-servo one is a table-driven unit test rather than an
evening with a screwdriver; a gesture's duration is knowable *before* it runs, which is what
lets the relax timer and the ``look_at`` cooldown derive one number instead of two that drift;
and preemption can be reasoned about, because a plan is a value.

**Why ``Axis`` is defined here rather than in ``core/hal.py``**, where the rest of the HAL
vocabulary lives and where §3.9.1 documents it. The ``layers`` contract puts ``core`` *above*
``domain``, and :class:`MotionGestureStarted` must name ``Axis`` to type its payload (§9.1.3) —
so defining it in ``core`` would make this module a ``domain -> core`` import and fail the P1
gate, in ``tests/domain/test_domain_purity.py`` before ``lint-imports`` even ran. ``core/hal.py``
re-exports it instead, so ``avid.core.hal.Axis`` remains the spelling every port and adapter
uses and the dependency rule keeps zero exceptions. Exactly the move ADR-013 made for ``BBox``
(§3.6.5); recorded for this one in SDS §3.9.4.

**Where the numbers live, and why they are here rather than in ``[motion]``.** How far a nod
dips and how long a leg takes are what a nod *is* — vocabulary, not deployment. What a *service*
owns is policy over time: when to relax, how often to allow a tool call, how far to drift when
idle. Those are config (`[motion]`, #200) because a rig or a room can reasonably change them;
the shape of a nod cannot change per deployment without ``nod`` meaning two things. The
signature admits a tuning parameter later without a caller change, the same way it admits
easing curves — and PMP §5.4 names easing curves as the exact rabbit hole this milestone
invites, so they are not here.

⚠️ **The direction convention is stated once, here, and it is not free to be ambiguous.**
Increasing degrees means **up** on a tilt axis and **left** on a pan axis (counter-clockwise
seen from above). ``BBox`` already carries the warning this is the other end of — *"an ambiguous
convention survives every unit test and surfaces months later as a servo aiming at the wrong
side of the room"*. A rig whose horn is mounted mirrored is corrected **mechanically**, by
reseating the horn on its spline at bring-up, not by inverting a sign here: an inverted sign is
invisible in a trace, and #207 is where the convention is confirmed against the actual head.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum, auto
from types import MappingProxyType
from typing import ClassVar

from avid.domain.events import Event

PAN = "pan"
TILT = "tilt"


@dataclass(frozen=True, slots=True, kw_only=True)
class Axis:
    """One servo axis a rig exposes — for capability negotiation (SDS §3.9.3, ADR-009).

    Read via :attr:`~avid.core.ports.Servo.axes` so the gesture engine stays axis-agnostic and
    the same code runs on a 1-servo rig, a 2-servo rig, or a simulator with six.

    ``name`` is the handle the domain uses — a :class:`Keyframe` addresses an axis by name, and
    which PCA9685 ``channel`` that lands on is the adapter's business. ``min_deg``/``max_deg``
    describe **the linkage's** reach; enforcing them is the *adapter's* job (see
    :meth:`~avid.core.ports.Servo.move_to`), and the planner plans inside them anyway — see
    :func:`plan`.
    """

    name: str
    channel: int
    min_deg: float
    max_deg: float

    @property
    def centre_deg(self) -> float:
        """The midpoint of the declared reach — this axis's resting angle."""
        return (self.min_deg + self.max_deg) / 2

    @property
    def half_span_deg(self) -> float:
        """Half the declared reach: the distance from :attr:`centre_deg` to either limit.

        Amplitudes are expressed as fractions of this rather than in degrees, so one gesture
        definition means the same *thing* on a wide pan and a narrow tilt instead of the same
        number of degrees — which on a narrow linkage would be the difference between a nod and
        driving the head into its stop.
        """
        return (self.max_deg - self.min_deg) / 2


class Gesture(Enum):
    """The gesture vocabulary: **intents**, never axis operations (SDS §3.9.3).

    Named for what the robot means, not for what a servo does — ``NOD``, not ``tilt_twice``.
    That is what lets the same value survive a rig change, and it is why the affect map (#202)
    and the ``look_at`` tool (#204) can both speak this vocabulary without either knowing how
    many servos exist.

    :attr:`CENTER` earns its place as the one gesture that is also a **resting state**: #203
    uses it to bring the head somewhere sensible before relaxing, so an idle robot is not
    frozen at whatever angle its last gesture happened to end on.

    :attr:`STEP_TOWARD` and :attr:`STEP_BACK` are the two translation intents ADR-015 admits
    (SDS §3.9.5) — a shuffle toward the user, a step back. They live in this enum because a
    step is an *intent* the same way a nod is, and one vocabulary is what lets a future trigger
    (#477) speak of "a gesture" without knowing whether it lands on a servo or a wheel. They
    are **not servo gestures**: :func:`plan` answers them with an empty tuple on every rig, and
    :func:`avid.domain.drive.plan_step` is the planner that realises them.
    """

    NOD = auto()
    SHAKE = auto()
    TURN_LEFT = auto()
    TURN_RIGHT = auto()
    LOOK_UP = auto()
    LOOK_DOWN = auto()
    CENTER = auto()
    STEP_TOWARD = auto()
    STEP_BACK = auto()


@dataclass(frozen=True, slots=True, kw_only=True)
class Keyframe:
    """One leg of a gesture: hold *axis* at *angle_deg*, arriving over *duration_ms*.

    Addressed by **axis name**, never by channel — the domain never learns a PCA9685 exists
    (SDS §3.9.4). ``duration_ms`` is the time to *reach* the angle, matching
    :meth:`~avid.core.ports.Servo.move_to`'s own parameter, so a plan translates to port calls
    one-for-one with no arithmetic in between.
    """

    axis: str
    angle_deg: float
    duration_ms: int


# --- the gesture vocabulary's shape (see the module docstring on why these are here) -------
#
# Fractions of an axis's half-span, so they mean the same gesture on any reach. Chosen to read
# at a metre, which is desk distance for this persona (SDS §2.4) — a gesture that is legible
# only up close is one nobody sees.

# A nod dips a third of the way down and returns, twice. Deliberately smaller than a turn: a
# nod is punctuation, and an emphatic one reads as a bow.
_NOD_FRACTION = 0.35
# The pan wiggle a nod degrades to on a rig with no tilt (§3.9.3). Smaller than _SHAKE_FRACTION
# on purpose — see _plan_nod for why the difference is load-bearing rather than cosmetic.
_NOD_FALLBACK_FRACTION = 0.15
# A shake swings wider than the fallback nod and is unmistakably lateral.
_SHAKE_FRACTION = 0.40
# A turn commits: most of the available reach, because a half-hearted turn reads as a flinch.
_TURN_FRACTION = 0.85
# Looking up or down goes most of the way and stays there — it is a pose, not a motion.
_LOOK_FRACTION = 0.80

# One leg of an oscillating gesture. ~220 ms is roughly a human head's beat; much faster reads
# as a twitch, much slower as a stretch.
_LEG_MS = 220
# A turn covers more ground, so it gets more time rather than more speed.
_TURN_MS = 500
# A look is shorter than a turn: less distance, and it is the head rather than the body.
_LOOK_MS = 450
# Returning to rest is unhurried — this is the move that precedes relaxing (#203).
_CENTER_MS = 400
# How many excursions each oscillating gesture makes. Two is the minimum that reads as
# deliberate: one is a twitch, and three starts to look like a malfunction.
_CYCLES = 2


def _axis(axes: Sequence[Axis], name: str) -> Axis | None:
    """The axis called *name*, or ``None`` if this rig has no such axis.

    The whole of capability negotiation, in one lookup. ``None`` is an ordinary answer here,
    not an error: §3.9.3's question is *"do I have a tilt axis?"* and *"no"* is a valid reply
    that every gesture below is written to handle.
    """
    return next((axis for axis in axes if axis.name == name), None)


def _at(axis: Axis, fraction: float, duration_ms: int) -> Keyframe:
    """A keyframe *fraction* of the half-span away from *axis*'s centre.

    Positive is toward the axis's maximum — up on tilt, left on pan — per the module
    docstring's convention. ``0.0`` is the centre, which is why the resting frame and the
    excursion frames come from the same expression rather than two.
    """
    return Keyframe(
        axis=axis.name,
        angle_deg=axis.centre_deg + fraction * axis.half_span_deg,
        duration_ms=duration_ms,
    )


def _oscillate(
    axis: Axis, *, fraction: float, leg_ms: int, alternate: bool
) -> tuple[Keyframe, ...]:
    """:data:`_CYCLES` excursions of *fraction* of the half-span, ending back at centre.

    Both repeating gestures are this shape, so it exists once — and the *difference* between
    them is the one parameter that matters:

    * ``alternate=False`` (a nod): away, back, away, back. Every excursion is to the **same**
      side, which on a tilt axis is a head dipping and returning. Down first, not up, because
      a nod that starts by rearing back reads as a flinch.
    * ``alternate=True`` (a shake): one side, then the other, then back to centre. A "no" is
      *lateral travel through* the centre; repeating one-sided excursions on a pan axis is a
      twitch, and it is a different message.
    """
    frames: list[Keyframe] = []
    if alternate:
        for i in range(_CYCLES * 2):
            frames.append(_at(axis, fraction if i % 2 == 0 else -fraction, leg_ms))
    else:
        for _ in range(_CYCLES):
            frames.append(_at(axis, -fraction, leg_ms))
            frames.append(_at(axis, 0.0, leg_ms))
    if frames[-1].angle_deg != axis.centre_deg:
        frames.append(_at(axis, 0.0, leg_ms))
    return tuple(frames)


def _plan_nod(axes: Sequence[Axis]) -> tuple[Keyframe, ...]:
    """§3.9.3's worked example: a real tilt if there is one, a small pan wiggle if not.

    ⚠️ **The fallback is smaller than a shake, and that gap is the design.** On a pan-only rig
    the only motion available is lateral, and lateral motion at a shake's amplitude means
    *"no"* — so a robot agreeing with you would be shaking its head. Keeping the fallback well
    under :data:`_SHAKE_FRACTION` makes it read as a small acknowledging sway rather than a
    denial. It is the honest compromise a 1 DoF rig allows, and it is exactly why ADR-009
    calls the second servo the thing that stops ``nod`` being a lie (SDS §3.9.4).
    """
    tilt = _axis(axes, TILT)
    if tilt is not None:
        return _oscillate(tilt, fraction=_NOD_FRACTION, leg_ms=_LEG_MS, alternate=False)
    pan = _axis(axes, PAN)
    if pan is not None:
        return _oscillate(
            pan, fraction=_NOD_FALLBACK_FRACTION, leg_ms=_LEG_MS, alternate=False
        )
    return ()


def _plan_pose(
    axes: Sequence[Axis], *, name: str, fraction: float, duration_ms: int
) -> tuple[Keyframe, ...]:
    """A single move to a held angle: a turn, or a look.

    *fraction* is signed — positive toward the axis's maximum (left, up), negative toward its
    minimum (right, down) — per the module docstring's convention. There is no return leg: the
    pose *is* the gesture, and the head stays there until something else moves it or #203's
    relax timer lets go.
    """
    axis = _axis(axes, name)
    if axis is None:
        return ()
    return (_at(axis, fraction, duration_ms),)


def plan(gesture: Gesture, axes: Sequence[Axis]) -> tuple[Keyframe, ...]:
    """The keyframes *gesture* becomes on a rig with *axes*. Pure (SDS §3.9.3, §3.9.4).

    Same inputs, same output: no clock, no randomness, no globals, no I/O. That is what makes
    the whole gesture engine testable with zero hardware, which is the trap M9 is otherwise
    prone to — bake the angles into the service and the only way to check a nod is to watch one.

    Angles are computed **inside each axis's declared reach**, never clamped into it. The
    adapter does clamp (§3.9.1, and it is genuinely its job), but a planner that leaned on that
    would produce gestures which look different on different rigs for reasons no test explains.
    The clamp stays a safety net rather than a control mechanism.

    **An empty tuple is a legitimate answer, not a failure** (AC-5). *"This rig cannot express
    that"* is a real thing for a robot to be, and #203 treats an empty plan as a no-op rather
    than an error: a 1-servo rig asked to look up should be quiet, not broken.

    ⚠️ **Degradation happens only where the degraded form does not invert the meaning.** ``NOD``
    falls back to a small pan wiggle because a diminished yes is still a yes. ``SHAKE`` does
    **not** fall back to tilt, and ``LOOK_UP`` does not fall back to pan: a lateral "no"
    performed vertically reads as agreement, and panning is not looking up. Silence is more
    honest than a gesture that means the opposite of what was asked.

    **A step is not a servo gesture.** ``STEP_TOWARD`` and ``STEP_BACK`` plan to ``()`` here on
    every rig, whatever axes it has — a head that leans forward is not a body that moves, and
    substituting one for the other is exactly the meaning inversion this docstring forbids.
    Their planner is :func:`avid.domain.drive.plan_step` (ADR-015).
    """
    match gesture:
        case Gesture.NOD:
            return _plan_nod(axes)
        case Gesture.SHAKE:
            pan = _axis(axes, PAN)
            if pan is None:
                return ()
            return _oscillate(
                pan, fraction=_SHAKE_FRACTION, leg_ms=_LEG_MS, alternate=True
            )
        case Gesture.TURN_LEFT:
            return _plan_pose(
                axes, name=PAN, fraction=_TURN_FRACTION, duration_ms=_TURN_MS
            )
        case Gesture.TURN_RIGHT:
            return _plan_pose(
                axes, name=PAN, fraction=-_TURN_FRACTION, duration_ms=_TURN_MS
            )
        case Gesture.LOOK_UP:
            return _plan_pose(
                axes, name=TILT, fraction=_LOOK_FRACTION, duration_ms=_LOOK_MS
            )
        case Gesture.LOOK_DOWN:
            return _plan_pose(
                axes, name=TILT, fraction=-_LOOK_FRACTION, duration_ms=_LOOK_MS
            )
        case Gesture.CENTER:
            return tuple(_at(axis, 0.0, _CENTER_MS) for axis in axes)
        case Gesture.STEP_TOWARD | Gesture.STEP_BACK:
            return ()


def duration_ms(keyframes: Iterable[Keyframe]) -> int:
    """How long *keyframes* take, in milliseconds — knowable **before** the gesture runs.

    One function rather than two call sites doing the same sum: #203's relax timer needs it to
    know when a gesture is over, and #204's cooldown needs it to know when the next one may be
    accepted. Derived twice, the two drift, and the symptom is a robot that relaxes mid-nod.
    """
    return sum(frame.duration_ms for frame in keyframes)


# --- the look_at vocabulary (#204, SDS §6.6) ---------------------------------
#
# A *direction* is not a gesture, and the distinction is why the tool is safe to hand to a
# model. `Gesture` includes NOD and SHAKE, which mean things; a `Direction` only points. The
# model may ask the robot to look somewhere and may not ask it to agree with someone.


class Direction(Enum):
    """Where the model may ask the robot to look (SDS §6.6).

    Deliberately a small closed set of **intents**, never an angle. The model states what it
    wants and §3.9.3 decides what that means in degrees on this rig — which is what keeps one
    tool working across the 2-servo robot, the 1-servo fallback and the fake, and what stops the
    model being handed a lever it can jam.

    ``CENTER`` is here because *"look at me"* and *"face forward"* are things people say, and
    because it is the one direction that always has somewhere to go.
    """

    LEFT = auto()
    RIGHT = auto()
    UP = auto()
    DOWN = auto()
    CENTER = auto()


class LookAtResult(Enum):
    """What became of a ``look_at`` request (SDS §6.6, §3.9.1).

    **Declining is a first-class outcome**, which is the whole reason this is an enum rather
    than a ``bool`` or a bare ``None``. §6.6 classifies the tool fire-and-forget, so the model
    never learns whether the servo arrived — but it must learn whether the robot *tried*, and
    why not if it did not. A silent no-op would leave the model believing it moved, and a robot
    that describes motion that never happened is worse than one that says it cannot.
    """

    ACCEPTED = auto()
    #: Inside ``[motion] look_at_cooldown_ms``. Not an error — the guard working (#204 AC-6).
    COOLING_DOWN = auto()
    #: This rig has no axis for that direction (``up`` on a pan-only robot). Honest, not silent.
    NO_AXIS = auto()


_DIRECTION_GESTURES: Mapping[Direction, Gesture] = MappingProxyType(
    {
        Direction.LEFT: Gesture.TURN_LEFT,
        Direction.RIGHT: Gesture.TURN_RIGHT,
        Direction.UP: Gesture.LOOK_UP,
        Direction.DOWN: Gesture.LOOK_DOWN,
        Direction.CENTER: Gesture.CENTER,
    }
)


def gesture_for_direction(direction: Direction) -> Gesture:
    """The gesture that realises *direction*. Pure, total, and indexes directly.

    Total over :class:`Direction` by an exhaustive test rather than by a default, for the reason
    every table in this project indexes directly: a sixth direction added later must be a build
    failure, not a robot that quietly ignores one word.

    Note what this mapping is *not*: it does not consult the rig. Whether the resulting gesture
    is expressible here is :func:`plan`'s answer, and the difference matters — *"there is no
    such direction"* and *"this robot cannot look up"* are different things to tell a model.
    """
    return _DIRECTION_GESTURES[direction]


# --- the three motion.* events (SDS §9.1.3, transcribed) ------------------------------------
#
# Transcription, not design: §9.1.3 already specifies publisher, subscribers, payload and
# overflow policy for all three, and CLAUDE.md §4 is blunt about what that means — *an event
# not in §9.1 does not exist*. ``motion`` was already in ``EVENT_DOMAINS`` and all three verbs
# end in "-ed", so the P4 validator accepts the names unchanged.
#
# These are **observability**. Nothing publishes "please nod": affect changes, MotionService
# subscribes, and it moves (SDS §3.5, §9.1.4). A gesture request would be a command wearing an
# event's clothes, and the servo call itself is a direct awaited port call because losing it
# would be a correctness bug rather than a cosmetic glitch.


@dataclass(frozen=True, slots=True, kw_only=True)
class MotionGestureStarted(Event):
    """A gesture began moving the rig (SDS §9.1.3). Published by ``MotionService``.

    ``axes`` is the axes this particular performance actually drives — the negotiated result,
    not the rig's inventory — so a log line distinguishes a real tilt nod from the pan-wiggle
    fallback without anyone re-deriving it from the plan.
    """

    name: ClassVar[str] = "motion.gesture_started"

    gesture: str
    axes: tuple[Axis, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class MotionGestureCompleted(Event):
    """A gesture ran to completion (SDS §9.1.3). Published by ``MotionService``.

    ``duration_ms`` is **measured**, not planned, and measured with ``monotonic_ns`` — the
    envelope carries both clocks precisely so this kind of arithmetic never touches wall time
    (§9.1.1). A gesture whose measured duration drifts from :func:`duration_ms`'s prediction is
    a loop under load, which is worth being able to see.
    """

    name: ClassVar[str] = "motion.gesture_completed"

    gesture: str
    duration_ms: int


@dataclass(frozen=True, slots=True, kw_only=True)
class MotionGesturePreempted(Event):
    """A gesture stopped before finishing (SDS §9.1.3). Published by ``MotionService``.

    ⚠️ **One event, two causes, and ``by`` is what tells them apart** — stated normatively in
    §9.1.3 rather than left to a reader:

    * ``by="nod"`` — a newer gesture interrupted this one. Ordinary, expected, and the whole
      point of preemption: the newest intent is the only one worth performing.
    * ``by=None`` — §3.12.3's I²C-fault abort. The rig failed mid-gesture, the service relaxed
      and carried on. *"Nothing except a bad API key at boot is allowed to stop the robot."*

    They are deliberately not two events. A subscriber counting interruptions wants both; one
    watching for hardware trouble filters on ``by is None``, and the field makes that a
    predicate rather than a correlation across two catalog rows.
    """

    name: ClassVar[str] = "motion.gesture_preempted"

    gesture: str
    by: str | None = None


__all__ = [
    "PAN",
    "TILT",
    "Axis",
    "Direction",
    "Gesture",
    "Keyframe",
    "MotionGestureCompleted",
    "MotionGesturePreempted",
    "MotionGestureStarted",
    "LookAtResult",
    "duration_ms",
    "gesture_for_direction",
    "plan",
]
