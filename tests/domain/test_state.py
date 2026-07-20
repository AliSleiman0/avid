"""Tests for ``RobotState`` and the §3.10.3 transition table (AVID-7).

Tier-1, pure, milliseconds (SDS §14.2): no async, no I/O, no clock. The load-bearing test
is ``test_no_undocumented_transitions`` — exhaustive over ``product(RobotState,
EVENT_TYPES)`` — which cashes SDS §3.4.2's claim that ~90% of bugs in a system like this
are illegal state transitions.
"""

from __future__ import annotations

import dataclasses
from itertools import product
from uuid import uuid4

import pytest

from avid.domain import (
    EVENT_TYPES,
    TRANSITION_TABLE,
    IllegalTransition,
    RobotState,
    StateTransitioned,
    Trigger,
    next_state,
    validate_event_name,
)


def make_state_transitioned(**overrides: object) -> StateTransitioned:
    fields: dict[str, object] = {
        "event_id": uuid4(),
        "correlation_id": uuid4(),
        "timestamp_ms": 1,
        "monotonic_ns": 2,
        "source": "StateManager",
        "from_": RobotState.BOOTING,
        "to": RobotState.IDLE,
        "trigger": Trigger.SYSTEM_STARTED,
    }
    fields.update(overrides)
    return StateTransitioned(**fields)  # type: ignore[arg-type]


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


# --- the state.transitioned event (AVID-69) ---------------------------------


def test_state_transitioned_carries_from_to_and_trigger() -> None:
    e = make_state_transitioned(
        from_=RobotState.SPEAKING,
        to=RobotState.LISTENING,
        trigger=Trigger.AUDIO_SPEECH_STARTED,
    )
    assert e.name == "state.transitioned"
    assert e.from_ is RobotState.SPEAKING
    assert e.to is RobotState.LISTENING
    assert e.trigger is Trigger.AUDIO_SPEECH_STARTED


def test_state_transitioned_is_frozen_slotted_kw_only() -> None:
    e = make_state_transitioned()
    assert not hasattr(e, "__dict__")  # slotted
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.to = RobotState.DEGRADED  # type: ignore[misc]
    with pytest.raises(TypeError):  # kw-only: positional construction rejected
        StateTransitioned(  # type: ignore[call-arg]
            uuid4(),
            uuid4(),
            1,
            2,
            "s",
            RobotState.BOOTING,
            RobotState.IDLE,
            Trigger.SYSTEM_STARTED,
        )


def test_state_transitioned_name_validates() -> None:
    """Its declared name passes the P4 validator (already run at class creation)."""
    validate_event_name(StateTransitioned.name)


def test_state_transitioned_carries_the_trigger_enum_not_its_name() -> None:
    """The §9.1.3 payload was corrected from ``trigger: str`` to ``trigger: Trigger``
    (AVID-69) so subscribers can ``match`` exhaustively instead of re-parsing a string.
    The dotted catalog name is still one attribute away for logs."""
    e = make_state_transitioned(trigger=Trigger.CONVERSATION_SESSION_LOST)
    assert isinstance(e.trigger, Trigger)
    assert e.trigger.value == "conversation.session_lost"


def test_state_transitioned_pairs_are_legal_by_construction() -> None:
    """Every documented row can be expressed as an event — the catalog payload and the
    §3.10.3 table agree on their vocabulary."""
    for frm, trigger, to in TRANSITION_CASES:
        e = make_state_transitioned(from_=frm, to=to, trigger=trigger)
        assert next_state(e.from_, e.trigger) == e.to
