"""The Tier-1 state-to-face table (AVID-72, SDS §6.8).

These tests moved down from ``tests/services/test_affect.py`` alongside the table itself:
``AffectService`` and ``ExpressionService`` both read it, so it belongs to neither of them.
Pure data and a pure lookup — no bus, no clock, no fixtures.
"""

from __future__ import annotations

import pytest

from avid.core.affect_map import TIER1, baseline_affect
from avid.domain import Affect, RobotState


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
