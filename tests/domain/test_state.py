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

    ⚠️ **This docstring used to claim LISTENING was absent on purpose, and that claim was
    false** (AVID-173). It read: *"the rising and falling edges strictly alternate … so a second
    rising edge from LISTENING is unreachable. An unreachable self-loop would be a lie in the
    normative table."* The alternation argument is correct **within ``AudioService``** and says
    nothing about where the *machine* is. AVID-162's recovery row lands in LISTENING, and if both
    of that turn's edges were spent while DEGRADED there is no falling edge left to move it on —
    so the next utterance's rising edge arrives in LISTENING. Traced on the Pi, 3 times in one
    180 s run. LISTENING is now a self-loop, and it is reachable, which is the test the table
    applies to itself.

    DEGRADED is a fifth source since AVID-162, but it does **not** open a turn — it absorbs the
    edge and stays put, because the session is still opening and may yet fail."""
    for opens in (RobotState.IDLE, RobotState.SLEEPING):
        assert next_state(opens, Trigger.AUDIO_SPEECH_STARTED) == RobotState.LISTENING
    for interrupts in (RobotState.SPEAKING, RobotState.THINKING):
        assert (
            next_state(interrupts, Trigger.AUDIO_SPEECH_STARTED) == RobotState.LISTENING
        )
    assert (
        next_state(RobotState.DEGRADED, Trigger.AUDIO_SPEECH_STARTED)
        == RobotState.DEGRADED
    )
    assert (
        next_state(RobotState.LISTENING, Trigger.AUDIO_SPEECH_STARTED)
        == RobotState.LISTENING
    )
    with pytest.raises(IllegalTransition):
        next_state(RobotState.BOOTING, Trigger.AUDIO_SPEECH_STARTED)


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


def test_the_recovery_arc_composes_while_the_user_is_still_talking() -> None:
    """AVID-162: the whole recovery turn, asserted as one journey.

    The rising edge asks for the reopen and is absorbed — the session is still opening and may
    yet fail — and the successful open rejoins the turn in LISTENING, where the user's own
    falling edge is waiting for it. Every trigger here after the first used to be illegal, which
    is what made the first turn after a drop stateless: no thinking face, no speaking face, and
    no state move behind a barge-in against that reply."""
    assert _walk(
        RobotState.DEGRADED,
        Trigger.AUDIO_SPEECH_STARTED,  # the edge that asks for the reopen
        Trigger.SYSTEM_DEGRADED_EXITED,  # open() succeeded: rejoin the turn
        Trigger.AUDIO_SPEECH_ENDED,  # the user finishes
        Trigger.AUDIO_PLAYBACK_STARTED,
        Trigger.AUDIO_PLAYBACK_FINISHED,
    ) == [
        RobotState.DEGRADED,
        RobotState.DEGRADED,
        RobotState.LISTENING,
        RobotState.THINKING,
        RobotState.SPEAKING,
        RobotState.IDLE,
    ]


def test_the_recovery_arc_composes_when_a_slow_open_outran_the_user() -> None:
    """The other half, and the common one: AVID-157 measured opens at 1.5-6.7 s, far longer
    than ``[gate] silence_hold_ms``, so the falling edge usually lands *during* the open.

    Recovery then rejoins a turn the user has already finished, and the reply is what moves the
    machine on — via ``LISTENING + playback_started``, the row AVID-161 added for the overlap.
    That row doing double duty here is why LISTENING works as the recovery target at all."""
    assert _walk(
        RobotState.DEGRADED,
        Trigger.AUDIO_SPEECH_STARTED,
        Trigger.AUDIO_SPEECH_ENDED,  # they finished before open() returned
        Trigger.SYSTEM_DEGRADED_EXITED,
        Trigger.AUDIO_PLAYBACK_STARTED,  # AVID-161's row carries it
        Trigger.AUDIO_PLAYBACK_FINISHED,
    ) == [
        RobotState.DEGRADED,
        RobotState.DEGRADED,
        RobotState.DEGRADED,
        RobotState.LISTENING,
        RobotState.SPEAKING,
        RobotState.IDLE,
    ]


def test_a_turn_that_ended_while_degraded_leaves_the_next_one_somewhere_legal() -> None:
    """AVID-173: the third continuation AVID-162 did not anticipate, walked whole.

    AVID-162 reasoned about two ways a recovery could continue — the user still talking
    (``LISTENING + speech_ended``) and a slow open outrun by them (``LISTENING +
    playback_started``). This is a third: **the turn ended entirely while degraded**, so both of
    its edges were absorbed and nothing is coming to move the machine off LISTENING. It parks
    there, and the *next* utterance's rising edge used to have no row — 3 occurrences in one
    180 s Pi run.

    Walked as a whole journey rather than asserted row-by-row, for the reason ``_walk`` exists:
    AVID-158, AVID-161 and this are all cases where each row was defensible alone and the
    composition dead-ended. Note the arc continues *past* the rising edge to a complete turn —
    stopping at the row under test is what let the previous recovery test miss this."""
    assert _walk(
        RobotState.DEGRADED,
        Trigger.AUDIO_SPEECH_STARTED,  # absorbed; open() starts
        Trigger.AUDIO_SPEECH_ENDED,  # absorbed; this turn is now OVER
        Trigger.SYSTEM_DEGRADED_EXITED,  # recovery parks in LISTENING
        Trigger.AUDIO_SPEECH_STARTED,  # the NEXT utterance — AVID-173's gap
        Trigger.AUDIO_SPEECH_ENDED,
        Trigger.AUDIO_PLAYBACK_STARTED,
        Trigger.AUDIO_PLAYBACK_FINISHED,
    ) == [
        RobotState.DEGRADED,
        RobotState.DEGRADED,
        RobotState.DEGRADED,
        RobotState.LISTENING,
        RobotState.LISTENING,  # the self-loop: already listening, still listening
        RobotState.THINKING,
        RobotState.SPEAKING,
        RobotState.IDLE,
    ]


def test_a_reply_arriving_after_the_machine_went_idle_still_composes() -> None:
    """AVID-189, observed on the M6 gate run — two playback edges in IDLE after a reconnect.

    The route to IDLE is the AVID-161 overlap doing what it was built for: recovery parks in
    LISTENING, the user speaks again (AVID-173's self-loop), their falling edge moves to THINKING,
    and a *stale* reply from before the drop drains — ``(THINKING, playback_finished) -> IDLE``.
    The machine is now idle with a reply still coming, and the real reply's ``playback_started``
    used to have nowhere to go.

    ⚠️ The arc deliberately continues **past** the row under test to a completed turn. Stopping at
    the gap is exactly how AVID-173 fixed the rising edge on this same arc and left the playback
    edges unrooted — the run that proved that fix is the run that exposed this one."""
    assert (
        _walk(
            RobotState.DEGRADED,
            Trigger.AUDIO_SPEECH_STARTED,  # absorbed while the socket is down
            Trigger.AUDIO_SPEECH_ENDED,  # absorbed; that turn is over
            Trigger.SYSTEM_DEGRADED_EXITED,  # reconnect -> LISTENING (AVID-162)
            Trigger.AUDIO_SPEECH_STARTED,  # they carry on talking (AVID-173's self-loop)
            Trigger.AUDIO_SPEECH_ENDED,
            Trigger.AUDIO_PLAYBACK_FINISHED,  # a pre-drop reply drains: THINKING -> IDLE
            Trigger.AUDIO_PLAYBACK_STARTED,  # AVID-189's gap: the real reply starts, from IDLE
            Trigger.AUDIO_PLAYBACK_FINISHED,  # ...and completes, through the SPEAKING row
        )
        == [
            RobotState.DEGRADED,
            RobotState.DEGRADED,
            RobotState.DEGRADED,
            RobotState.LISTENING,
            RobotState.LISTENING,
            RobotState.THINKING,
            RobotState.IDLE,
            RobotState.SPEAKING,
            RobotState.IDLE,
        ]
    )


def test_playback_finished_in_idle_stays_illegal() -> None:
    """The row AVID-189 deliberately did **not** add.

    The gate logged *two* illegal transitions and the obvious response is two rows. But with
    ``(IDLE, playback_started) -> SPEAKING`` in place, the machine is in SPEAKING when the reply
    drains, so ``(SPEAKING, playback_finished) -> IDLE`` already carries the second edge — and an
    ``(IDLE, playback_finished)`` row would be **unreachable**.

    ``domain/state.py``'s own rule is that an unreachable row is a lie, and AVID-173 had just
    finished correcting that mistake in the other direction. This test is what stops someone
    "completing" the fix by adding it."""
    with pytest.raises(IllegalTransition):
        next_state(RobotState.IDLE, Trigger.AUDIO_PLAYBACK_FINISHED)


def test_the_think_timeout_arc_degrades_and_the_next_turn_recovers() -> None:
    """AVID-171 / SDS §6.9: the turn whose first token never arrives, as one journey.

    The row ``(THINKING, THINK_TIMEOUT) -> DEGRADED`` has existed since AVID-7 and **nothing
    could reach it** — the timer was never wired. The bench paid for that difference: a 60 ms
    noise blip opened a session the model never answered, and the robot sat in THINKING for
    **54 seconds** with no row out, because the only other thing watching that silence was the
    idle close, which tears the socket down and drives no transition at all.

    The tail is deliberately the full recovery: degrading is only worth anything if the next
    utterance gets the robot back, and that half rides AVID-162's row unchanged."""
    assert (
        _walk(
            RobotState.IDLE,
            Trigger.AUDIO_SPEECH_STARTED,  # the blip
            Trigger.AUDIO_SPEECH_ENDED,  # ...and the falling edge that arms the deadline
            Trigger.THINK_TIMEOUT,  # no first token: give up on a socket that is still open
            Trigger.AUDIO_SPEECH_STARTED,  # the user tries again — absorbed while open() runs
            Trigger.SYSTEM_DEGRADED_EXITED,  # the reopen succeeded
            Trigger.AUDIO_SPEECH_ENDED,
            Trigger.AUDIO_PLAYBACK_STARTED,
            Trigger.AUDIO_PLAYBACK_FINISHED,
        )
        == [
            RobotState.IDLE,
            RobotState.LISTENING,
            RobotState.THINKING,
            RobotState.DEGRADED,
            RobotState.DEGRADED,
            RobotState.LISTENING,
            RobotState.THINKING,
            RobotState.SPEAKING,
            RobotState.IDLE,
        ]
    )


def test_a_proactive_turn_the_network_refused_still_has_a_way_out() -> None:
    """#452: the arc with no user in it, and the reason the deadline moved to the state.

    ``BehaviorService`` drives ``IDLE -> THINKING`` before publishing ``behavior.trigger_fired``,
    so a refused ``open()`` leaves the machine in THINKING with **no falling edge ever coming** —
    no one is in the room, and the two remaining THINKING rows both need a live session. Walked
    from a state the table can reach without a single audio event, this arc has exactly one
    continuation, and it is the one the timer drives.

    Its sibling above walks the same escape from a *reactive* turn. The point of having both is
    that the row is reachable from every path into THINKING, which is what the fix asserts and
    what falling-edge arming quietly did not provide."""
    assert (
        _walk(
            RobotState.IDLE,
            Trigger.BEHAVIOR_TRIGGER_FIRED,  # a reminder fires; the session is never opened
            Trigger.THINK_TIMEOUT,  # ...and this is the only thing that can move it
            Trigger.AUDIO_SPEECH_STARTED,  # the owner walks in and speaks: absorbed while open() runs
            Trigger.SYSTEM_DEGRADED_EXITED,
        )
        == [
            RobotState.IDLE,
            RobotState.THINKING,
            RobotState.DEGRADED,
            RobotState.DEGRADED,
            RobotState.LISTENING,
        ]
    )


def test_a_failed_reopen_leaves_the_robot_degraded_rather_than_lying() -> None:
    """The asymmetry that makes the LISTENING target safe: nothing moves off DEGRADED except a
    *successful* open. If ``open()`` raises there is no ``degraded_exited``, the user's edges are
    absorbed, and the robot still says it is broken — which it is."""
    assert _walk(
        RobotState.DEGRADED,
        Trigger.AUDIO_SPEECH_STARTED,
        Trigger.AUDIO_SPEECH_ENDED,
    ) == [RobotState.DEGRADED, RobotState.DEGRADED, RobotState.DEGRADED]


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
