"""The step vocabulary, the pure step planner, and the three ``drive.*`` events (#400, ADR-015).

Tier-1 unit tests: no clock, no I/O, no async, no motor. The two claims SDS §3.9.5 stakes
itself on — **net-zero by construction** and **bounded by the budget** — are properties of a
plan, so they are asserted here on values rather than confirmed on a bench. A rig with no
wheels and a gesture that is not a step both answer with an empty plan, exactly the shape
§3.9.4 established for a 1-servo rig asked to look up.
"""

from __future__ import annotations

import dataclasses
import random
from typing import ClassVar
from uuid import uuid4

import pytest

from avid.core.hal import DriveCapabilities as HalDriveCapabilities
from avid.domain import (
    ABORT_REASONS,
    EDGE,
    STEP_GESTURES,
    DriveCapabilities,
    DriveStepAborted,
    DriveStepCompleted,
    DriveStepStarted,
    Event,
    Gesture,
    Heading,
    Leg,
    StepGeometry,
    homing_leg,
    net_mm,
    peak_excursion_mm,
    plan,
    plan_step,
    step_duration_ms,
)
from avid.domain.events import validate_event_name
from tests.domain.test_motion import _PAN_TILT

# The shipped geometry (config/*.toml since #400's schema PR), spelled out here rather than
# loaded, because a domain test must not read a file — and because the relation the tests
# assert (step <= budget) is what matters, not the numbers.
_CAPS = DriveCapabilities(mm_per_s_at_full=128.0)
_GEOMETRY = StepGeometry(
    step_mm=20.0, max_excursion_mm=30.0, speed_frac=0.4, dwell_ms=300
)
# A geometry whose step exceeds its budget. Not reachable through config (DriveConfig
# refuses it), but plan_step is a pure function and must clamp rather than trust.
_OVERREACH = StepGeometry(
    step_mm=50.0, max_excursion_mm=30.0, speed_frac=0.4, dwell_ms=300
)
_NO_DWELL = StepGeometry(
    step_mm=20.0, max_excursion_mm=30.0, speed_frac=0.4, dwell_ms=0
)

_ENVELOPE_FIELDS = frozenset(
    {"event_id", "correlation_id", "timestamp_ms", "monotonic_ns", "source"}
)


def _envelope(source: str = "DriveService") -> dict[str, object]:
    return {
        "event_id": uuid4(),
        "correlation_id": uuid4(),
        "timestamp_ms": 1,
        "monotonic_ns": 2,
        "source": source,
    }


def _moving(legs: tuple[Leg, ...]) -> list[Leg]:
    return [leg for leg in legs if leg.heading is not None]


# --- DriveCapabilities: the value that lives in the domain (ADR-013's shape, again) --------


def test_drive_capabilities_is_the_same_class_under_both_spellings() -> None:
    """``avid.core.hal.DriveCapabilities`` is a re-export, not a copy — ``is``, for the reason
    ``Axis`` and ``BBox`` are tested the same way: two identical dataclasses would pass every
    other test here and still break ``isinstance`` across the port seam."""
    assert HalDriveCapabilities is DriveCapabilities


def test_capabilities_geometry_and_leg_are_frozen_slotted_and_kw_only() -> None:
    """Every value crossing a port matches the ``Event`` envelope's rules (CLAUDE.md §3)."""
    for value in (
        _CAPS,
        _GEOMETRY,
        Leg(heading=Heading.FORWARD, speed_frac=0.4, duration_ms=1, distance_mm=1.0),
    ):
        assert not hasattr(value, "__dict__"), type(value)
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, dataclasses.fields(value)[0].name, None)
    with pytest.raises(TypeError):
        DriveCapabilities(128.0)  # type: ignore[misc]  # kw_only


# --- plan_step(): totality, purity, and the two load-bearing properties -------------------


@pytest.mark.parametrize("gesture", list(Gesture), ids=lambda g: g.name)
@pytest.mark.parametrize("caps", [_CAPS, None], ids=["wheels", "no-wheels"])
def test_plan_step_answers_for_every_gesture_with_and_without_wheels(
    gesture: Gesture, caps: DriveCapabilities | None
) -> None:
    """Total over the enum × {wheels, no wheels}. Iterating ``Gesture`` means a tenth gesture
    is a failing test here rather than a surprise on the Pi."""
    assert isinstance(plan_step(gesture, caps, _GEOMETRY), tuple)


@pytest.mark.parametrize("gesture", list(Gesture), ids=lambda g: g.name)
def test_a_rig_without_wheels_plans_nothing_for_any_gesture(gesture: Gesture) -> None:
    """``caps is None`` is *"this rig has no wheels"*: a legitimate answer, not an error, and
    the reason a laptop, a wheel-less rig and the shipped ``drive = "fake"`` need no caller
    changes anywhere (SDS §3.9.3)."""
    assert plan_step(gesture, None, _GEOMETRY) == ()


@pytest.mark.parametrize(
    "gesture",
    sorted(set(Gesture) - STEP_GESTURES, key=lambda g: g.name),
    ids=lambda g: g.name,
)
def test_a_servo_gesture_is_not_a_step(gesture: Gesture) -> None:
    """A nod is not a step, and this planner does not pretend — the mirror image of
    ``motion.plan`` answering the step gestures with ``()``."""
    assert plan_step(gesture, _CAPS, _GEOMETRY) == ()


@pytest.mark.parametrize(
    "gesture", sorted(STEP_GESTURES, key=lambda g: g.name), ids=lambda g: g.name
)
def test_a_step_gesture_plans_to_nothing_on_every_servo_rig(gesture: Gesture) -> None:
    """The other half of that mirror: ``motion.plan`` never turns a step into a head movement.
    A head that leans forward is not a body that moves (SDS §3.9.4's meaning-inversion rule)."""
    assert plan(gesture, _PAN_TILT) == ()
    assert plan(gesture, ()) == ()


def test_the_two_step_gestures_are_exactly_the_step_set() -> None:
    assert STEP_GESTURES == {Gesture.STEP_TOWARD, Gesture.STEP_BACK}


@pytest.mark.parametrize(
    "gesture", sorted(STEP_GESTURES, key=lambda g: g.name), ids=lambda g: g.name
)
def test_plan_step_is_deterministic_and_does_not_mutate_its_inputs(
    gesture: Gesture,
) -> None:
    """Same inputs, same output — the randomness of *when* to step lives in the service."""
    before = dataclasses.astuple(_GEOMETRY), dataclasses.astuple(_CAPS)
    assert plan_step(gesture, _CAPS, _GEOMETRY) == plan_step(gesture, _CAPS, _GEOMETRY)
    assert (dataclasses.astuple(_GEOMETRY), dataclasses.astuple(_CAPS)) == before


@pytest.mark.parametrize(
    "gesture", sorted(STEP_GESTURES, key=lambda g: g.name), ids=lambda g: g.name
)
def test_a_step_is_out_dwell_back_and_the_two_moving_legs_are_mirror_images(
    gesture: Gesture,
) -> None:
    """The shape the net-zero claim rests on: the return leg is in the **same tuple** as the
    out leg, equal in length and opposite in heading. A service cannot perform one and forget
    the other — it performs a plan."""
    legs = plan_step(gesture, _CAPS, _GEOMETRY)
    assert [leg.heading is None for leg in legs] == [False, True, False]
    out, dwell, back = legs
    assert out.distance_mm == -back.distance_mm
    assert out.duration_ms == back.duration_ms
    assert out.speed_frac == back.speed_frac == _GEOMETRY.speed_frac
    assert dwell.duration_ms == _GEOMETRY.dwell_ms
    assert dwell.distance_mm == 0.0 and dwell.wheels == (0.0, 0.0)


def test_toward_goes_forward_first_and_back_goes_backward_first() -> None:
    """The heading of the *out* leg is the gesture's meaning; the return leg only undoes it."""
    toward = _moving(plan_step(Gesture.STEP_TOWARD, _CAPS, _GEOMETRY))
    back = _moving(plan_step(Gesture.STEP_BACK, _CAPS, _GEOMETRY))
    assert toward[0].heading is Heading.FORWARD and toward[0].distance_mm > 0
    assert back[0].heading is Heading.BACKWARD and back[0].distance_mm < 0


def test_forward_is_positive_on_both_wheels_at_the_port() -> None:
    """``+`` means *toward the user* on every rig; the measured flip is the adapter's (§3.9.5).
    A straight step drives both wheels identically — the port never sees a pivot from here."""
    out = _moving(plan_step(Gesture.STEP_TOWARD, _CAPS, _GEOMETRY))[0]
    assert out.wheels == (_GEOMETRY.speed_frac, _GEOMETRY.speed_frac)
    back = _moving(plan_step(Gesture.STEP_BACK, _CAPS, _GEOMETRY))[0]
    assert back.wheels == (-_GEOMETRY.speed_frac, -_GEOMETRY.speed_frac)


@pytest.mark.parametrize(
    "gesture", sorted(STEP_GESTURES, key=lambda g: g.name), ids=lambda g: g.name
)
@pytest.mark.parametrize(
    "geometry",
    [_GEOMETRY, _OVERREACH, _NO_DWELL],
    ids=["shipped", "overreach", "no-dwell"],
)
def test_every_step_plan_is_net_zero(gesture: Gesture, geometry: StepGeometry) -> None:
    """**Net-zero by construction** (#400 AC-4). The plan contains its own undoing."""
    assert net_mm(plan_step(gesture, _CAPS, geometry)) == 0.0


def test_any_sequence_of_step_plans_sums_to_zero() -> None:
    """The thirty-day claim: error cannot accumulate over idle steps, in any order, however
    many. Seeded so a failure reproduces; a hundred random sequences because the property is
    about *sequences*, not about one plan."""
    rng = random.Random(400)
    gestures = sorted(STEP_GESTURES, key=lambda g: g.name)
    for _ in range(100):
        sequence = [rng.choice(gestures) for _ in range(rng.randint(1, 40))]
        total = sum(net_mm(plan_step(g, _CAPS, _GEOMETRY)) for g in sequence)
        assert total == 0.0, sequence


@pytest.mark.parametrize(
    "gesture", sorted(STEP_GESTURES, key=lambda g: g.name), ids=lambda g: g.name
)
def test_the_excursion_of_a_plan_is_one_leg_and_never_exceeds_the_budget(
    gesture: Gesture,
) -> None:
    """**Bounded** (#400 AC-5, the budget half). The furthest the robot gets from origin while
    running a plan is exactly one leg, and a leg is at most the budget."""
    legs = plan_step(gesture, _CAPS, _GEOMETRY)
    assert peak_excursion_mm(legs) == _GEOMETRY.step_mm
    assert peak_excursion_mm(legs) <= _GEOMETRY.max_excursion_mm


@pytest.mark.parametrize(
    "gesture", sorted(STEP_GESTURES, key=lambda g: g.name), ids=lambda g: g.name
)
def test_a_step_longer_than_the_budget_is_clamped_inside_the_planner(
    gesture: Gesture,
) -> None:
    """The planner does not trust the config validator that also refuses this. A pure function
    that assumed its inputs were validated would walk the robot past its budget the day someone
    constructs the geometry by hand — and F-14 says the budget is the return leg's only defence.

    Neuter: drop the ``min(...)`` in ``plan_step`` and this reads 50.0 against a 30.0 budget."""
    legs = plan_step(gesture, _CAPS, _OVERREACH)
    assert peak_excursion_mm(legs) == _OVERREACH.max_excursion_mm
    assert net_mm(legs) == 0.0


def test_a_zero_dwell_is_omitted_rather_than_emitted_as_an_empty_leg() -> None:
    """A plan never carries a member that does nothing — a service iterating legs would
    otherwise wait on a zero-length dwell and a reader would wonder why."""
    legs = plan_step(Gesture.STEP_TOWARD, _CAPS, _NO_DWELL)
    assert [leg.heading for leg in legs] == [Heading.FORWARD, Heading.BACKWARD]


def test_leg_duration_is_distance_over_speed_and_never_zero() -> None:
    """20 mm at 0.4 × 128 mm/s is ~391 ms; the arithmetic is stated so a reader can check it,
    and a vanishingly short leg still takes 1 ms — a zero-duration leg is a jump."""
    out = _moving(plan_step(Gesture.STEP_TOWARD, _CAPS, _GEOMETRY))[0]
    assert out.duration_ms == round(20.0 / (0.4 * 128.0) * 1000)
    tiny = StepGeometry(step_mm=1e-6, max_excursion_mm=30.0, speed_frac=1.0, dwell_ms=0)
    assert all(
        leg.duration_ms >= 1 for leg in plan_step(Gesture.STEP_BACK, _CAPS, tiny)
    )


def test_step_duration_is_the_sum_of_the_legs_and_an_empty_plan_takes_no_time() -> None:
    legs = plan_step(Gesture.STEP_TOWARD, _CAPS, _GEOMETRY)
    assert step_duration_ms(legs) == sum(leg.duration_ms for leg in legs)
    assert step_duration_ms(legs) == 2 * legs[0].duration_ms + _GEOMETRY.dwell_ms
    assert step_duration_ms(()) == 0


# --- homing_leg(): an abort is a delay, not a drift ------------------------------------------


@pytest.mark.parametrize("offset", [7.5, -12.0], ids=["ahead", "behind"])
def test_a_homing_leg_undoes_the_offset_exactly(offset: float) -> None:
    """A step cut short leaves the robot off-origin by the part it did not run; the homing
    leg heads the opposite way by the same amount, so the next step starts from zero."""
    (leg,) = homing_leg(offset, _CAPS, _GEOMETRY)
    assert leg.distance_mm == -offset
    assert leg.heading is (Heading.BACKWARD if offset > 0 else Heading.FORWARD)
    assert (
        net_mm(
            (Leg(heading=None, speed_frac=0.0, duration_ms=0, distance_mm=offset), leg)
        )
        == 0.0
    )


def test_a_homing_leg_is_clamped_to_the_budget() -> None:
    """An offset past the budget is the thing the budget says cannot happen; the honest reply
    is to move at most one budget's worth and let the odometer say what is left."""
    (leg,) = homing_leg(90.0, _CAPS, _GEOMETRY)
    assert leg.distance_mm == -_GEOMETRY.max_excursion_mm


def test_no_offset_means_no_homing_leg() -> None:
    assert homing_leg(0.0, _CAPS, _GEOMETRY) == ()


# --- the three drive.* events (SDS §9.1.3) ----------------------------------------------------


@pytest.mark.parametrize(
    "event_type",
    [DriveStepStarted, DriveStepCompleted, DriveStepAborted],
    ids=lambda t: t.name,
)
def test_each_event_name_is_the_catalog_row_and_passes_the_p4_validator(
    event_type: type[Event],
) -> None:
    """Transcription, not design: §9.1.3 specifies all three, ``drive`` is the tenth domain,
    and every verb ends in "-ed"."""
    name: ClassVar[str] = event_type.name
    assert name.startswith("drive.step_")
    validate_event_name(event_type.name)


def test_step_started_carries_the_out_legs_heading_and_length() -> None:
    event = DriveStepStarted(
        **_envelope(), gesture="step_toward", heading="forward", distance_mm=20.0
    )
    assert (event.gesture, event.heading, event.distance_mm) == (
        "step_toward",
        "forward",
        20.0,
    )
    assert _ENVELOPE_FIELDS <= {f.name for f in dataclasses.fields(event)}


def test_step_completed_carries_a_measured_duration_and_the_plans_net() -> None:
    """``net_mm`` rides the event so a completed step whose payload says anything but ``0.0``
    is visibly a defect rather than a quiet drift."""
    event = DriveStepCompleted(
        **_envelope(), gesture="step_back", duration_ms=1082, net_mm=0.0
    )
    assert event.duration_ms == 1082 and event.net_mm == 0.0


@pytest.mark.parametrize("reason", ABORT_REASONS)
def test_step_aborted_constructs_for_every_documented_reason(reason: str) -> None:
    """A closed vocabulary, not a name-or-``None``: the four causes want four responses, and
    ``edge`` is the one Observability logs at WARNING."""
    assert (
        DriveStepAborted(**_envelope(), gesture="step_toward", reason=reason).reason
        == reason
    )
    assert EDGE in ABORT_REASONS


@pytest.mark.parametrize(
    "event_type",
    [DriveStepStarted, DriveStepCompleted, DriveStepAborted],
    ids=lambda t: t.name,
)
def test_each_event_is_frozen_slotted_and_kw_only(event_type: type[Event]) -> None:
    payloads: dict[str, object] = {
        "drive.step_started": {
            "gesture": "step_toward",
            "heading": "forward",
            "distance_mm": 20.0,
        },
        "drive.step_completed": {
            "gesture": "step_toward",
            "duration_ms": 1,
            "net_mm": 0.0,
        },
        "drive.step_aborted": {"gesture": "step_toward", "reason": EDGE},
    }
    event = event_type(**_envelope(), **payloads[event_type.name])  # type: ignore[arg-type]
    assert not hasattr(event, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.gesture = "nod"  # type: ignore[misc]
