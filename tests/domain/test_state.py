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


def test_the_turn_end_row_is_our_own_falling_edge() -> None:
    """LISTENING + ``audio.speech_ended`` -> THINKING (AVID-158) — the edge §3.10.1's diagram
    has always labelled "turn end detected". It is emphatically **not**
    ``conversation.user_transcribed``: that is the model's separate transcription pass, and on
    hardware it arrives after the assistant's audio has already started."""
    assert (
        next_state(RobotState.LISTENING, Trigger.AUDIO_SPEECH_ENDED)
        == RobotState.THINKING
    )


def test_a_resumed_utterance_returns_to_listening_from_thinking() -> None:
    """THINKING + speech_started -> LISTENING (AVID-158): the user starts a fresh burst before
    the reply begins. Without this row the corrected turn-end edge would merely relocate the
    wedge from LISTENING to THINKING."""
    assert (
        next_state(RobotState.THINKING, Trigger.AUDIO_SPEECH_STARTED)
        == RobotState.LISTENING
    )


def test_speech_started_is_legal_from_every_state_a_turn_can_begin_in() -> None:
    """The invariant ``AudioService._begin_speech``'s comment claims: one trigger covers every
    legal source of a rising edge — IDLE and SLEEPING (a turn opens), SPEAKING (barge-in) and,
    since AVID-158, THINKING (the user resumes before the reply).

    LISTENING is **absent on purpose**: the rising and falling edges strictly alternate
    (``_run`` calls ``_begin_speech`` only when ``not self._speaking``, and only
    ``_end_speech`` clears that), so a second rising edge from LISTENING is unreachable. It
    appeared five times in the AVID-158 bench trace *only* because the falling edge did not
    leave LISTENING. An unreachable self-loop would be a lie in the normative table."""
    for opens in (RobotState.IDLE, RobotState.SLEEPING):
        assert next_state(opens, Trigger.AUDIO_SPEECH_STARTED) == RobotState.LISTENING
    for interrupts in (RobotState.SPEAKING, RobotState.THINKING):
        assert (
            next_state(interrupts, Trigger.AUDIO_SPEECH_STARTED) == RobotState.LISTENING
        )
    for refuses in (RobotState.BOOTING, RobotState.LISTENING, RobotState.DEGRADED):
        with pytest.raises(IllegalTransition):
            next_state(refuses, Trigger.AUDIO_SPEECH_STARTED)


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


def _walk(start: RobotState, *triggers: Trigger) -> list[RobotState]:
    """Fold *triggers* through the table from *start*, returning every state visited.

    Raises ``IllegalTransition`` at the first gap, which is the point: these arcs are asserted
    as *whole journeys*, because AVID-158 and AVID-161 were both cases where each row looked
    defensible alone and the composition dead-ended."""
    state = start
    visited = [state]
    for trigger in triggers:
        state = next_state(state, trigger)
        visited.append(state)
    return visited


def test_the_ordinary_turn_arc_still_composes() -> None:
    """The regression guard for every table change: IDLE round-trip, untouched."""
    assert _walk(
        RobotState.IDLE,
        Trigger.AUDIO_SPEECH_STARTED,
        Trigger.AUDIO_SPEECH_ENDED,
        Trigger.AUDIO_PLAYBACK_STARTED,
        Trigger.AUDIO_PLAYBACK_FINISHED,
    ) == [
        RobotState.IDLE,
        RobotState.LISTENING,
        RobotState.THINKING,
        RobotState.SPEAKING,
        RobotState.IDLE,
    ]


def test_the_overlap_arc_composes_when_the_user_stops_first() -> None:
    """AVID-161: the model answers an earlier commit while the user is still talking (measured
    0.9 s before our falling edge), the user finishes, then the overlapping reply drains.

    Asserted as a journey, not as rows. The point of the THINKING hop is that it *ends* back at
    IDLE — a SPEAKING self-loop would look just as reasonable row-by-row and strand the machine."""
    assert _walk(
        RobotState.LISTENING,  # the user is mid-utterance
        Trigger.AUDIO_PLAYBACK_STARTED,  # the reply to an EARLIER commit starts
        Trigger.AUDIO_SPEECH_ENDED,  # the user stops first
        Trigger.AUDIO_PLAYBACK_FINISHED,  # the reply drains
    ) == [
        RobotState.LISTENING,
        RobotState.SPEAKING,
        RobotState.THINKING,
        RobotState.IDLE,
    ]


def test_the_overlap_arc_composes_when_the_user_barges_in() -> None:
    """The other half: the user is loud enough, ``AudioService`` cuts playback (which drives no
    transition of its own), they finish, and the *next* reply must still be able to play.

    This is what the SPEAKING self-loop would have broken — after it the machine sits in
    SPEAKING and the following ``audio.playback_started`` has no row."""
    assert _walk(
        RobotState.LISTENING,
        Trigger.AUDIO_PLAYBACK_STARTED,  # overlap begins; interrupt() drives nothing
        Trigger.AUDIO_SPEECH_ENDED,  # the user finishes
        Trigger.AUDIO_PLAYBACK_STARTED,  # the NEXT reply — the row that must exist
        Trigger.AUDIO_PLAYBACK_FINISHED,
    ) == [
        RobotState.LISTENING,
        RobotState.SPEAKING,
        RobotState.THINKING,
        RobotState.SPEAKING,
        RobotState.IDLE,
    ]


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


def test_trigger_has_exactly_the_documented_event_column() -> None:
    """``Trigger`` *is* the §3.10.3 "Event" column, so its membership is pinned here the same
    way ``RobotState``'s is above.

    This is the assertion that holds AVID-158's deletion: ``CONVERSATION_USER_TRANSCRIBED`` is
    gone from the column. The *event* is still published and still catalogued in §9.1.3 — it
    just no longer drives the machine, because it arrives too late to."""
    assert {t.name for t in Trigger} == {
        "SYSTEM_STARTED",
        "AUDIO_SPEECH_STARTED",
        "AUDIO_SPEECH_ENDED",
        "BEHAVIOR_TRIGGER_FIRED",
        "PRESENCE_LOST_TIMEOUT",
        "VISION_PRESENCE_GAINED",
        "LISTEN_TIMEOUT",
        "AUDIO_PLAYBACK_STARTED",
        "THINK_TIMEOUT",
        "AUDIO_PLAYBACK_FINISHED",
        "CONVERSATION_SESSION_LOST",
        "SYSTEM_DEGRADED_EXITED",
    }


def test_every_trigger_drives_at_least_one_row() -> None:
    """No orphan members. A trigger with no row is not an Event-column entry at all — it is
    seven guaranteed-illegal pairs padding ``test_no_undocumented_transitions`` and a value
    ``StateTransitioned.trigger`` can never legally carry.

    Rows without a *driver* are fine and expected (``BEHAVIOR_TRIGGER_FIRED`` is M6, the three
    ``timer.*`` expiries are unwired): membership tracks the table, not the call sites. This is
    the invariant that made AVID-158 delete a member rather than leave it row-less."""
    driven = {trigger for _, trigger in TRANSITION_TABLE}
    assert driven == set(EVENT_TYPES), (
        f"triggers with no row: {sorted(t.name for t in set(EVENT_TYPES) - driven)}"
    )


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
