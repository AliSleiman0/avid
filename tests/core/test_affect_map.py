"""The two affect-policy tables: state to face (AVID-72, SDS §6.8) and affect to gesture (#202).

These tests moved down from ``tests/services/test_affect.py`` alongside the table itself:
``AffectService`` and ``ExpressionService`` both read it, so it belongs to neither of them.
Pure data and a pure lookup — no bus, no clock, no fixtures.
"""

from __future__ import annotations

import pytest

from avid.core.affect_map import GESTURES, TIER1, baseline_affect, gesture_for
from avid.domain import Affect, Gesture, RobotState


def test_tier1_covers_the_robot_state_enum_exactly() -> None:
    """Exhaustive on purpose, the same bargain ``test_no_undocumented_transitions`` makes: a
    new ``RobotState`` without a face fails here, in milliseconds, rather than raising inside
    a bus handler on the Pi. There is no default entry precisely so this test is load-bearing.
    """
    assert set(TIER1) == set(RobotState)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (RobotState.IDLE, Affect.IDLE),
        (RobotState.LISTENING, Affect.LISTENING),
        (RobotState.THINKING, Affect.THINKING),
        (RobotState.SPEAKING, Affect.SPEAKING),
        (RobotState.SLEEPING, Affect.SLEEPING),
        (RobotState.BOOTING, Affect.IDLE),
        (RobotState.DEGRADED, Affect.IDLE),
    ],
)
def test_each_state_maps_to_its_documented_face(
    state: RobotState, expected: Affect
) -> None:
    """Spelled out row by row so a reviewer can diff the table against SDS §6.8 directly."""
    assert baseline_affect(state) is expected


def test_tier1_values_are_all_real_affects() -> None:
    assert all(isinstance(value, Affect) for value in TIER1.values())


def test_the_table_is_frozen() -> None:
    """A ``MappingProxyType``, like ``TRANSITION_TABLE``: a normative table nobody can edit
    at runtime. Without this, a single stray assignment anywhere in the process silently
    rewrites what every face means."""
    with pytest.raises(TypeError):
        TIER1[RobotState.IDLE] = Affect.SAD  # type: ignore[index]  # that is the point


def test_an_unmapped_state_raises_rather_than_defaulting() -> None:
    """The no-default policy, asserted rather than assumed. ``baseline_affect`` must fail
    loudly on a state it has never heard of — a silent IDLE would mean a robot wearing the
    wrong face forever with nothing in the logs to say so."""

    class _Unmapped:
        """Not a ``RobotState`` at all — the cheapest possible stand-in for tomorrow's."""

    with pytest.raises(KeyError):
        baseline_affect(_Unmapped())  # type: ignore[arg-type]  # deliberately unmapped


# --- affect -> gesture (#202, SDS §3.7.2) ------------------------------------


def test_gestures_covers_the_affect_enum_exactly() -> None:
    """Total over ``Affect``, iterated rather than listed.

    A ninth affect added later is a failing test here rather than a ``KeyError`` on the Pi at
    1 a.m. — and the failure mode this specifically prevents is the quiet one: a robot that
    simply stops gesturing for one affect looks exactly like a robot whose servo came
    unplugged."""
    assert set(GESTURES) == set(Affect)


def test_happy_nods_and_that_row_is_normative() -> None:
    """§3.7.2's arrow, spelled out on its own.

    *"`affect.changed` → … → HAPPY → nod"* is the fan-out that justified the event bus in the
    first place: one publish, the face changes **and** the servo nods, and `ConversationService`
    never learned that a servo exists. It is the gate's first clause reduced to one assertion."""
    assert gesture_for(Affect.HAPPY) is Gesture.NOD


@pytest.mark.parametrize(
    ("affect", "expected"),
    [
        (Affect.HAPPY, Gesture.NOD),
        (Affect.SAD, Gesture.LOOK_DOWN),
        (Affect.CONFUSED, Gesture.LOOK_UP),
    ],
    ids=["happy", "sad", "confused"],
)
def test_each_tier_two_overlay_maps_to_its_documented_gesture(
    affect: Affect, expected: Gesture
) -> None:
    """Spelled out row by row so a reviewer can diff the table against the docstring's
    reasoning directly — the same shape ``TIER1``'s row-by-row test takes.

    ⚠️ ``CONFUSED`` is the one worth reading twice. The quizzical head tilt everyone pictures
    is a **roll**, and this rig is pan + tilt (ADR-009) — a third axis is not in the milestone
    and §7.3 does not buy one. Looking *up* is the closest honest reading on the axes that
    exist, not a substitute for the gesture that does not."""
    assert gesture_for(affect) is expected


@pytest.mark.parametrize(
    "affect",
    [Affect.IDLE, Affect.LISTENING, Affect.THINKING, Affect.SPEAKING],
    ids=lambda a: a.name.lower(),
)
def test_the_tier_one_baselines_move_nothing(affect: Affect) -> None:
    """**``None`` is the common answer, and that is the design** (AC-2/AC-4).

    ``affect.changed`` fires on every state transition, so a gesture on each of these would
    have the servos running continuously through a conversation — failing the gate's *"relaxes
    when idle"* clause **by construction** and putting sustained load on the rail #206 is
    measuring. These four are the face's job, not the body's."""
    assert gesture_for(affect) is None


def test_sleeping_does_not_gesture_because_sleep_is_the_absence_of_one() -> None:
    """#203 handles sleep by *relaxing*, which is the opposite of moving.

    A gesture here would re-energise the servos at the exact moment the robot is supposed to
    go quiet — the one failure a person hears from across a room."""
    assert gesture_for(Affect.SLEEPING) is None


def test_most_affects_map_to_nothing() -> None:
    """Asserted as a shape, not just member by member.

    A future edit that made gesturing the default would satisfy every row above except the
    four Tier-1 ones and still produce a twitchy robot. Stating the ratio makes the *policy*
    — restraint — testable rather than implied."""
    moved = [affect for affect in Affect if gesture_for(affect) is not None]
    assert len(moved) < len(Affect) / 2


def test_no_affect_steps_in_v1() -> None:
    """ADR-015 admits the wheels and deliberately maps **no affect** to them.

    The restraint argument above, applied to a louder actuator: an affect that stepped would
    put a gearbox in the room on every mood change. Idle drift is the only trigger in v1; the
    first candidate for a second (#477) is designed on the bus side, not in this table. Asserted
    so that adding ``HAPPY -> STEP_TOWARD`` one evening is a red test, not a surprise."""
    from avid.domain import STEP_GESTURES

    assert not {gesture_for(affect) for affect in Affect} & STEP_GESTURES


def test_the_gesture_table_is_frozen() -> None:
    """A normative table with a second, mutable edition is not normative — the same reason
    ``TIER1`` and ``TRANSITION_TABLE`` are ``MappingProxyType``."""
    with pytest.raises(TypeError):
        GESTURES[Affect.IDLE] = Gesture.NOD  # type: ignore[index]


def test_an_unmapped_affect_raises_rather_than_silently_never_moving() -> None:
    """Indexes directly — no ``.get(..., None)``.

    A default of ``None`` is the dangerous one here precisely *because* ``None`` is a valid
    answer for most affects: the mistake would be indistinguishable from the design, forever."""

    class _Unmapped:
        pass

    with pytest.raises(KeyError):
        gesture_for(_Unmapped())  # type: ignore[arg-type]


def test_the_table_names_no_operational_state() -> None:
    """Affect and ``RobotState`` stay orthogonal (§3.10.1, CLAUDE.md §5).

    This table maps affect to gesture and never consults operational state; ``MotionService``
    reads ``state.transitioned`` separately for its relax logic, exactly as
    ``ExpressionService`` reads it separately for the baseline face. Asserted here because the
    *other* table in this very module is a ``RobotState`` map, and the temptation to reach for
    it from ten lines away is real."""
    assert all(isinstance(key, Affect) for key in GESTURES)
    assert not set(GESTURES) & set(TIER1)
