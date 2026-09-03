"""The gesture vocabulary, the pure planner, and the three ``motion.*`` events (#201).

Tier-1 unit tests: no clock, no I/O, no async, no hardware. That this file *can* be written
this way is the milestone's central bet — §3.9.3 keeps the gesture in the domain as an intent
and negotiates the realization from the rig's own report, so *"a nod is a real tilt on two
servos and degrades to a pan wiggle on one"* is a table here rather than an evening with a
screwdriver.

Three rigs are exercised throughout, and the two degenerate ones are not padding: the 1-servo
fallback is what ADR-009 retained as a **capability path**, so a fallback nobody exercises is a
fallback nobody has (SDS §3.9.4).
"""

from __future__ import annotations

import dataclasses
from typing import ClassVar
from uuid import uuid4

import pytest

from avid.core.hal import Axis as HalAxis
from avid.domain import (
    PAN,
    TILT,
    Axis,
    Event,
    Gesture,
    Keyframe,
    MotionGestureCompleted,
    MotionGesturePreempted,
    MotionGestureStarted,
    duration_ms,
    plan,
)
from avid.domain.events import validate_event_name

# The reaches config/*.toml ships since #200 — deliberately asymmetric and deliberately not
# 0–180, so a planner that assumed a full sweep or a shared span fails here rather than on the
# bench.
_PAN_AXIS = Axis(name=PAN, channel=0, min_deg=30.0, max_deg=150.0)
_TILT_AXIS = Axis(name=TILT, channel=13, min_deg=60.0, max_deg=120.0)

_PAN_TILT = (_PAN_AXIS, _TILT_AXIS)
_PAN_ONLY = (_PAN_AXIS,)
_TILT_ONLY = (_TILT_AXIS,)

_RIGS = {"pan+tilt": _PAN_TILT, "pan-only": _PAN_ONLY, "tilt-only": _TILT_ONLY}

# The gestures a servo can express. ``STEP_*`` are intents in the same enum (ADR-015) but
# they are the wheels' to realise — ``plan`` answers them with ``()`` on every rig, so a
# duration assertion over them would be asserting that nothing takes time.
_SERVO_GESTURES = [
    g for g in Gesture if g not in (Gesture.STEP_TOWARD, Gesture.STEP_BACK)
]

_ENVELOPE_FIELDS = frozenset(
    {"event_id", "correlation_id", "timestamp_ms", "monotonic_ns", "source"}
)


def _envelope(source: str = "MotionService") -> dict[str, object]:
    return {
        "event_id": uuid4(),
        "correlation_id": uuid4(),
        "timestamp_ms": 1,
        "monotonic_ns": 2,
        "source": source,
    }


# --- Axis: the value that moved into the domain (ADR-013's shape, SDS §3.9.4) -


def test_axis_is_the_same_class_under_both_spellings() -> None:
    """``avid.core.hal.Axis`` is a re-export, not a copy.

    The declaration moved into ``domain/`` so ``motion.gesture_started`` can name it without
    the project's first ``domain -> core`` import (P1); every port and adapter still spells it
    ``avid.core.hal.Axis``. ``is`` rather than structural equality, because two identical
    dataclasses would satisfy every other test in this file and still break ``isinstance``
    across the seam — which is the failure a re-export exists to prevent."""
    assert HalAxis is Axis


def test_axis_is_frozen_slotted_and_kw_only() -> None:
    """Matching every other HAL value type and the ``Event`` envelope. Handlers dispatch
    concurrently, so a mutable value crossing a port is a data race with extra steps."""
    axis = Axis(name=PAN, channel=0, min_deg=0.0, max_deg=180.0)
    assert not hasattr(axis, "__dict__")  # slots
    with pytest.raises(dataclasses.FrozenInstanceError):
        axis.channel = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        Axis(PAN, 0, 0.0, 180.0)  # type: ignore[misc]  # kw_only


@pytest.mark.parametrize(
    ("axis", "centre", "half_span"),
    [(_PAN_AXIS, 90.0, 60.0), (_TILT_AXIS, 90.0, 30.0)],
    ids=["pan", "tilt"],
)
def test_centre_and_half_span_come_from_the_declared_reach(
    axis: Axis, centre: float, half_span: float
) -> None:
    """The two derived numbers every amplitude is expressed against.

    Both axes centre on 90° here and their half-spans differ by 2× — which is the case that
    matters. An amplitude in *degrees* would mean two different gestures on these two axes; as
    a fraction of the half-span it means the same thing on both."""
    assert axis.centre_deg == centre
    assert axis.half_span_deg == half_span


def test_keyframe_is_frozen_slotted_kw_only_and_names_an_axis_not_a_channel() -> None:
    """The domain never learns a PCA9685 exists (SDS §3.9.4).

    ``axis`` is a name because that is the only handle a *gesture* can meaningfully hold: which
    channel "tilt" is wired to is a fact about a solder joint, and putting it here would make
    every plan rig-specific."""
    frame = Keyframe(axis=TILT, angle_deg=75.0, duration_ms=220)
    assert frame.axis == "tilt"
    assert not hasattr(frame, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.angle_deg = 0.0  # type: ignore[misc]
    with pytest.raises(TypeError):
        Keyframe(TILT, 75.0, 220)  # type: ignore[misc]
    assert not hasattr(frame, "channel")


# --- plan(): totality, purity, and staying inside the reach -------------------


@pytest.mark.parametrize("rig", list(_RIGS), ids=list(_RIGS))
@pytest.mark.parametrize("gesture", list(Gesture), ids=lambda g: g.name)
def test_plan_answers_for_every_gesture_on_every_rig(
    gesture: Gesture, rig: str
) -> None:
    """Total over the enum × the three rig shapes — every member, none of which may raise.

    Iterating ``Gesture`` rather than listing members means a gesture added later is a
    failing test here rather than a ``KeyError`` on the Pi at 1 a.m. — the lesson
    ``ExpressionService``'s face cache already encodes. The two step intents (ADR-015) are
    covered by this: they must answer, and their answer on a servo rig is ``()``."""
    assert isinstance(plan(gesture, _RIGS[rig]), tuple)


@pytest.mark.parametrize("rig", list(_RIGS), ids=list(_RIGS))
@pytest.mark.parametrize("gesture", list(Gesture), ids=lambda g: g.name)
def test_every_keyframe_lands_inside_its_own_axiss_declared_reach(
    gesture: Gesture, rig: str
) -> None:
    """The planner plans **inside** the reach; it does not lean on the adapter's clamp.

    The adapter does clamp, and that is genuinely its job (SDS §3.9.1) — but a planner relying
    on it produces gestures that look different on different rigs for reasons no test explains,
    because the clamp silently truncates the shape rather than scaling it. Here the clamp stays
    a safety net.

    The two axes have deliberately different spans (120° and 60°), so a planner that computed
    absolute degrees, or used the wrong axis's span, overshoots tilt and this fails."""
    by_name = {axis.name: axis for axis in _RIGS[rig]}
    for frame in plan(gesture, _RIGS[rig]):
        axis = by_name[frame.axis]
        assert axis.min_deg <= frame.angle_deg <= axis.max_deg, (gesture, frame)


@pytest.mark.parametrize("rig", list(_RIGS), ids=list(_RIGS))
@pytest.mark.parametrize("gesture", list(Gesture), ids=lambda g: g.name)
def test_every_keyframe_has_a_positive_duration(gesture: Gesture, rig: str) -> None:
    """A zero-duration leg is a jump, and a negative one is an argument the adapter would
    turn into a division by zero when it computes its step count."""
    for frame in plan(gesture, _RIGS[rig]):
        assert frame.duration_ms > 0, (gesture, frame)


@pytest.mark.parametrize("gesture", list(Gesture), ids=lambda g: g.name)
def test_plan_is_deterministic(gesture: Gesture) -> None:
    """Same inputs, same output — no clock, no randomness, no globals (AC-4).

    #205's idle drift *is* randomised, and this is the test that keeps that randomness in the
    service where it belongs: a random planner would make every gesture assertion above a
    flake, and the failures would look like hardware."""
    assert plan(gesture, _PAN_TILT) == plan(gesture, _PAN_TILT)


def test_plan_does_not_mutate_the_axes_it_is_given() -> None:
    """Pure in the other direction too: the rig's report is an input, not a workspace."""
    axes = (_PAN_AXIS, _TILT_AXIS)
    before = dataclasses.astuple(axes[0]), dataclasses.astuple(axes[1])
    plan(Gesture.NOD, axes)
    assert (dataclasses.astuple(axes[0]), dataclasses.astuple(axes[1])) == before


# --- the negotiated realization: which rig can express what -------------------

# Spelled out as a table so the *policy* is reviewable in one place, rather than inferred from
# seven branches. An empty tuple is a legitimate answer (AC-5), and which combinations are
# empty is a design decision that deserves to be visible when it changes.
_EXPRESSIBLE = [
    ("nod is a tilt on the full rig", Gesture.NOD, _PAN_TILT, TILT),
    ("nod degrades to pan without tilt", Gesture.NOD, _PAN_ONLY, PAN),
    ("nod is a tilt on a tilt-only rig", Gesture.NOD, _TILT_ONLY, TILT),
    ("shake is pan", Gesture.SHAKE, _PAN_TILT, PAN),
    ("shake needs pan", Gesture.SHAKE, _TILT_ONLY, None),
    ("turn needs pan", Gesture.TURN_LEFT, _TILT_ONLY, None),
    ("turn is pan", Gesture.TURN_RIGHT, _PAN_ONLY, PAN),
    ("look needs tilt", Gesture.LOOK_UP, _PAN_ONLY, None),
    ("look is tilt", Gesture.LOOK_DOWN, _TILT_ONLY, TILT),
    ("center moves whatever exists", Gesture.CENTER, _PAN_ONLY, PAN),
    # ADR-015: a step is not a servo gesture on ANY rig. A head that leans forward is not a
    # body that moves, and substituting one for the other is the meaning inversion the
    # planner's docstring forbids. The wheels' planner is avid.domain.drive.plan_step.
    ("a step is never a head movement", Gesture.STEP_TOWARD, _PAN_TILT, None),
    ("nor is stepping back", Gesture.STEP_BACK, _PAN_TILT, None),
]


@pytest.mark.parametrize(
    ("case_id", "gesture", "axes", "expected_axis"),
    _EXPRESSIBLE,
    ids=[case[0] for case in _EXPRESSIBLE],
)
def test_the_rig_a_gesture_lands_on_is_negotiated_not_assumed(
    case_id: str, gesture: Gesture, axes: tuple[Axis, ...], expected_axis: str | None
) -> None:
    """§3.9.3's negotiation, as a table.

    ``expected_axis is None`` means *"this rig cannot express that"* — an empty plan, which
    #203 treats as a no-op rather than an error. A 1-servo rig asked to look up should be
    quiet, not broken."""
    frames = plan(gesture, axes)
    if expected_axis is None:
        assert frames == (), case_id
        return
    assert frames, case_id
    assert {frame.axis for frame in frames} == {expected_axis}, case_id


def test_a_rig_with_neither_axis_expresses_nothing_at_all() -> None:
    """The degenerate rig. Not reachable through config since #200 rejects an empty ``axes``,
    but ``plan`` is a pure function and must answer rather than raise — an exception here would
    reach the bus as ``system.handler_failed`` for a robot that is merely unable, not broken."""
    unknown = (Axis(name="roll", channel=7, min_deg=80.0, max_deg=100.0),)
    for gesture in Gesture:
        if gesture is Gesture.CENTER:
            continue  # CENTER rests whatever axes exist, whatever they are called
        assert plan(gesture, unknown) == (), gesture
    assert plan(Gesture.NOD, ()) == ()


def test_the_nod_fallback_is_quieter_than_a_shake_so_it_does_not_read_as_no() -> None:
    """⚠️ The gap between the two pan amplitudes is the design, not a coincidence.

    On a pan-only rig the only motion available is lateral, and lateral motion at a shake's
    amplitude *means* "no" — so a robot agreeing with you would be shaking its head. The
    fallback stays well under it, reading as a small acknowledging sway. It is the honest
    compromise 1 DoF allows, and it is exactly why ADR-009 calls the second servo the thing
    that stops ``nod`` being a lie (SDS §3.9.4).

    Asserted as a relation between the two plans rather than against the constants, so tuning
    either one cannot silently close the gap."""
    nod = plan(Gesture.NOD, _PAN_ONLY)
    shake = plan(Gesture.SHAKE, _PAN_ONLY)
    centre = _PAN_AXIS.centre_deg
    nod_swing = max(abs(frame.angle_deg - centre) for frame in nod)
    shake_swing = max(abs(frame.angle_deg - centre) for frame in shake)
    assert 0 < nod_swing < shake_swing / 2


def test_a_nod_returns_to_the_same_side_and_a_shake_crosses_the_centre() -> None:
    """The two oscillations differ in shape, not only in size.

    A nod dips and returns, twice, always to the same side — a head bobbing. A "no" is lateral
    travel *through* the centre: one side, then the other. Repeating one-sided excursions on a
    pan axis is a twitch, which is a different message from a refusal, and both would satisfy a
    test that only checked "it moved on pan"."""
    tilt_centre = _TILT_AXIS.centre_deg
    nod_offsets = [f.angle_deg - tilt_centre for f in plan(Gesture.NOD, _PAN_TILT)]
    assert all(offset <= 0 for offset in nod_offsets), nod_offsets

    pan_centre = _PAN_AXIS.centre_deg
    shake_offsets = [f.angle_deg - pan_centre for f in plan(Gesture.SHAKE, _PAN_TILT)]
    assert any(offset > 0 for offset in shake_offsets)
    assert any(offset < 0 for offset in shake_offsets)


@pytest.mark.parametrize("gesture", [Gesture.NOD, Gesture.SHAKE], ids=["nod", "shake"])
def test_an_oscillating_gesture_ends_where_it_started(gesture: Gesture) -> None:
    """A repeating gesture leaves the head at rest.

    Otherwise every nod would ratchet the head a little further from centre, and #203's relax
    timer would let go of it there. A pose gesture (``TURN_*``, ``LOOK_*``) deliberately does
    *not* return — the pose is the point — which is why this covers only the two that repeat."""
    frames = plan(gesture, _PAN_TILT)
    axis = next(a for a in _PAN_TILT if a.name == frames[-1].axis)
    assert frames[-1].angle_deg == axis.centre_deg


def test_center_rests_every_axis_the_rig_has() -> None:
    """``CENTER`` is the one gesture that is also a resting state (#203 uses it before relaxing).

    Every axis, not just the one a previous gesture happened to move: a robot that centres its
    head and leaves its body turned is a robot that looks like it is listening to someone else."""
    frames = plan(Gesture.CENTER, _PAN_TILT)
    assert {f.axis for f in frames} == {PAN, TILT}
    for frame in frames:
        axis = next(a for a in _PAN_TILT if a.name == frame.axis)
        assert frame.angle_deg == axis.centre_deg


# --- duration_ms(): one number, derived once ---------------------------------


def test_duration_is_the_sum_of_the_legs() -> None:
    """Knowable **before** the gesture runs, which is the point (AC-6)."""
    frames = (
        Keyframe(axis=TILT, angle_deg=80.0, duration_ms=220),
        Keyframe(axis=TILT, angle_deg=90.0, duration_ms=180),
    )
    assert duration_ms(frames) == 400


def test_an_empty_plan_takes_no_time() -> None:
    """The arithmetic companion to "empty is a legitimate answer": #203 must be able to ask how
    long a no-op takes without special-casing it."""
    assert duration_ms(()) == 0


@pytest.mark.parametrize("gesture", _SERVO_GESTURES, ids=lambda g: g.name)
def test_every_expressible_gesture_has_a_knowable_positive_duration(
    gesture: Gesture,
) -> None:
    """#203's relax timer and #204's cooldown both derive from this one function.

    Two call sites computing it separately is how they drift, and the symptom of the drift is a
    robot that relaxes mid-nod — which reads as a hardware fault. Over the *servo* gestures:
    a step's duration is ``avid.domain.drive.step_duration_ms``'s to know."""
    assert duration_ms(plan(gesture, _PAN_TILT)) > 0


# --- the three motion.* events (SDS §9.1.3) -----------------------------------


@pytest.mark.parametrize(
    "event_type",
    [MotionGestureStarted, MotionGestureCompleted, MotionGesturePreempted],
    ids=lambda t: t.name,
)
def test_each_event_name_is_the_catalog_row_and_passes_the_p4_validator(
    event_type: type[Event],
) -> None:
    """Transcription, not design: §9.1.3 specifies all three and CLAUDE.md §4 says an event not
    in §9.1 does not exist. ``motion`` was already in ``EVENT_DOMAINS`` and all three verbs end
    in "-ed", so no allowlist entry was needed."""
    name: ClassVar[str] = event_type.name
    assert name.startswith("motion.gesture_")
    validate_event_name(event_type.name)


def test_gesture_started_carries_the_axes_it_actually_drives() -> None:
    """``axes`` is the negotiated result, not the rig's inventory.

    That distinction is what makes the event useful: on a pan-only rig a nod publishes ``pan``,
    so one log line separates a real tilt nod from the fallback without anyone re-deriving it
    from the plan. It is also why the payload needed ``Axis`` in the domain at all."""
    event = MotionGestureStarted(
        **_envelope(), gesture=Gesture.NOD.name.lower(), axes=(_TILT_AXIS,)
    )
    assert event.gesture == "nod"
    assert event.axes == (_TILT_AXIS,)
    assert _ENVELOPE_FIELDS <= {f.name for f in dataclasses.fields(event)}


def test_gesture_completed_carries_a_measured_duration() -> None:
    """Measured, and measured with ``monotonic_ns`` (§9.1.1) — the service's job, but the field
    is an ``int`` of milliseconds so there is one obvious way to fill it."""
    event = MotionGestureCompleted(**_envelope(), gesture="nod", duration_ms=880)
    assert event.duration_ms == 880


def test_preempted_constructs_in_both_of_its_two_documented_shapes() -> None:
    """⚠️ One event, two causes, and ``by`` is what tells them apart (§9.1.3).

    ``by="nod"`` is a newer gesture interrupting an older one — ordinary, and the whole point of
    preemption. ``by=None`` is §3.12.3's I²C-fault abort: the rig failed mid-gesture, the
    service relaxed and carried on. Both are asserted because they are genuinely different
    facts that happen to share a row, and a reader who assumes ``by`` is always a gesture name
    writes a subscriber that crashes the first time hardware misbehaves."""
    interrupted = MotionGesturePreempted(**_envelope(), gesture="nod", by="shake")
    aborted = MotionGesturePreempted(**_envelope(), gesture="nod", by=None)
    assert interrupted.by == "shake"
    assert aborted.by is None
    # The fault shape is the one a caller is most likely to forget to pass, so it is the default.
    assert MotionGesturePreempted(**_envelope(), gesture="nod").by is None


@pytest.mark.parametrize(
    "event_type",
    [MotionGestureStarted, MotionGestureCompleted, MotionGesturePreempted],
    ids=lambda t: t.name,
)
def test_each_event_is_frozen_slotted_and_kw_only(event_type: type[Event]) -> None:
    """The envelope's own rules, inherited: handlers dispatch concurrently, so a mutable event
    is a data race with extra steps (CLAUDE.md §3)."""
    payloads: dict[str, object] = {
        "motion.gesture_started": {"gesture": "nod", "axes": (_TILT_AXIS,)},
        "motion.gesture_completed": {"gesture": "nod", "duration_ms": 1},
        "motion.gesture_preempted": {"gesture": "nod", "by": None},
    }
    event = event_type(**_envelope(), **payloads[event_type.name])  # type: ignore[arg-type]
    assert not hasattr(event, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.gesture = "shake"  # type: ignore[misc]
