"""Tests for ``RobotState`` and the §3.10.3 transition table (AVID-7).

Tier-1, pure, milliseconds (SDS §14.2): no async, no I/O, no clock. The load-bearing test
is ``test_no_undocumented_transitions`` — exhaustive over ``product(RobotState,
EVENT_TYPES)`` — which cashes SDS §3.4.2's claim that ~90% of bugs in a system like this
are illegal state transitions.
"""

from __future__ import annotations

from itertools import product

import pytest

from avid.domain import (
    EVENT_TYPES,
    TRANSITION_TABLE,
    IllegalTransition,
    RobotState,
    Trigger,
    next_state,
)

# Derived from the table itself so there is exactly one source of truth: every documented
# transition becomes a parametrised case, and nothing can be tested that isn't in the table.
TRANSITION_CASES = [
    (frm, trigger, to) for (frm, trigger), to in TRANSITION_TABLE.items()
]


# --- the documented transitions ---------------------------------------------


@pytest.mark.parametrize("frm,trigger,expected", TRANSITION_CASES)
def test_transition_table(
    frm: RobotState, trigger: Trigger, expected: RobotState
) -> None:
    assert next_state(frm, trigger) == expected


def test_no_undocumented_transitions() -> None:
    """Every ``(state, trigger)`` pair not in §3.10.3 raises ``IllegalTransition``."""
    for state, trigger in product(RobotState, EVENT_TYPES):
        if (state, trigger) not in TRANSITION_TABLE:
            with pytest.raises(IllegalTransition):
                next_state(state, trigger)


# --- rows worth spotlighting individually -----------------------------------


def test_barge_in_row() -> None:
    """SPEAKING + speech_started -> LISTENING — the row that separates a companion from a
    kiosk (SDS §3.10.3). Distinct from SPEAKING + playback_finished -> IDLE."""
    assert (
        next_state(RobotState.SPEAKING, Trigger.AUDIO_SPEECH_STARTED)
        == RobotState.LISTENING
    )
    assert (
        next_state(RobotState.SPEAKING, Trigger.AUDIO_PLAYBACK_FINISHED)
        == RobotState.IDLE
    )


def test_session_lost_degrades_from_any_state() -> None:
    """The "*any* state" row: session_lost -> DEGRADED holds for all seven states,
    including DEGRADED itself (an intentional idempotent self-loop)."""
    for state in RobotState:
        assert (
            next_state(state, Trigger.CONVERSATION_SESSION_LOST) == RobotState.DEGRADED
        )


def test_boot_reaches_idle() -> None:
    """The transition AVID-14's composition root depends on: BOOTING -> IDLE."""
    assert next_state(RobotState.BOOTING, Trigger.SYSTEM_STARTED) == RobotState.IDLE


def test_next_state_is_pure() -> None:
    """Same inputs, same output, no side effects — calling twice cannot differ, and the
    frozen table is unchanged."""
    before = dict(TRANSITION_TABLE)
    first = next_state(RobotState.IDLE, Trigger.AUDIO_SPEECH_STARTED)
    second = next_state(RobotState.IDLE, Trigger.AUDIO_SPEECH_STARTED)
    assert first == second == RobotState.LISTENING
    assert dict(TRANSITION_TABLE) == before


def test_transition_table_is_read_only() -> None:
    """It is a frozen mapping (SDS §3.10.3) — the normative table cannot be mutated."""
    with pytest.raises(TypeError):
        TRANSITION_TABLE[  # type: ignore[index]  # asserting the mapping is read-only
            (RobotState.IDLE, Trigger.SYSTEM_STARTED)
        ] = RobotState.DEGRADED


def test_illegal_transition_names_state_and_trigger() -> None:
    exc = IllegalTransition(RobotState.SPEAKING, Trigger.SYSTEM_STARTED)
    assert exc.state == RobotState.SPEAKING
    assert exc.trigger == Trigger.SYSTEM_STARTED
    assert "SPEAKING" in str(exc)
    assert "SYSTEM_STARTED" in str(exc)


def test_robotstate_has_exactly_the_seven_documented_members() -> None:
    assert {s.name for s in RobotState} == {
        "BOOTING",
        "IDLE",
        "LISTENING",
        "THINKING",
        "SPEAKING",
        "SLEEPING",
        "DEGRADED",
    }
