"""``RobotState`` and the normative transition table (AVID-7, SDS §3.10).

The robot's **operational** state machine. ``RobotState`` is *not* emotion — that is
``Affect`` (AVID-8), and the two are deliberately **orthogonal** (SDS §3.10.1): the robot
can be SLEEPING-and-content or LISTENING-and-confused. Nothing in this module may couple
them; conflating them is the most common design error in this class of project.

Every legal transition is one row in :data:`TRANSITION_TABLE` (frozen, matching SDS §3.10.3
exactly); :func:`next_state` is a **pure function** over it — no I/O, no clock, no globals.
Any ``(state, trigger)`` pair absent from the table raises :class:`IllegalTransition`:
loudly in tests, logged-and-ignored in production (SDS §3.10.3) — that policy is the
*caller's*; the domain just raises. The exhaustive ``test_no_undocumented_transitions``
cashes SDS §3.4.2's claim that ~90% of bugs here are illegal transitions, in ~4 ms.

**Guards live with the caller, not here.** Rows like "not quiet hours" or "all adapters
healthy" (§3.10.3) need a clock or config and would break purity, so :func:`next_state`
answers only *is this transition legal, and what is the target* — the service that drives
the machine evaluates the guard before attempting it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto
from types import MappingProxyType
from typing import ClassVar

from avid.domain.events import Event


class RobotState(Enum):
    """The robot's operational state (SDS §3.10.1). Orthogonal to ``Affect`` (AVID-8)."""

    BOOTING = auto()
    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()
    SLEEPING = auto()
    DEGRADED = auto()


class Trigger(Enum):
    """What can drive a state transition (the SDS §3.10.3 "Event" column).

    Most triggers are bus events, valued here with their catalog ``<domain>.<verb>`` name
    so a service can later map an incoming event to a trigger by name — a convenience, not
    a mapping this module owns. Three triggers are *not* bus events but timer expiries
    (``timer.*``): the 10-minute presence-lost nap, the 30 s listen timeout, the 10 s think
    timeout. Modelling them as first-class triggers is what lets the table — and its
    exhaustive test — stay a closed, pure set.

    Membership tracks the **table**, not the call sites: several rows have no driver yet
    (``BEHAVIOR_TRIGGER_FIRED`` is M6; ``PRESENCE_LOST_TIMEOUT`` and ``LISTEN_TIMEOUT`` are still
    unwired), and that is fine — but a member with *no row* is not an Event-column entry at all,
    just seven guaranteed-illegal pairs and a false claim in :class:`StateTransitioned`'s payload
    type. ``THINK_TIMEOUT`` was in that unwired set until AVID-171: the row existed and nothing
    could reach it, and the bench paid for the difference — a 60 ms noise blip opened a session the
    model never answered and the robot sat in THINKING for **54 seconds** with no row out. It is
    driven by ``ConversationService`` now (SDS §6.9).
    ``test_every_trigger_drives_at_least_one_row`` holds that line; it is why AVID-158 deleted
    ``CONVERSATION_USER_TRANSCRIBED`` outright rather than leaving it row-less. That fact is
    still published (``avid/domain/conversation.py``) — it simply drives nothing.
    """

    SYSTEM_STARTED = "system.started"
    AUDIO_SPEECH_STARTED = "audio.speech_started"
    AUDIO_SPEECH_ENDED = "audio.speech_ended"
    BEHAVIOR_TRIGGER_FIRED = "behavior.trigger_fired"
    PRESENCE_LOST_TIMEOUT = "timer.presence_lost"  # IDLE, sustained 10 min
    VISION_PRESENCE_GAINED = "vision.presence_gained"
    LISTEN_TIMEOUT = "timer.listen_timeout"  # LISTENING, 30 s
    AUDIO_PLAYBACK_STARTED = "audio.playback_started"
    THINK_TIMEOUT = "timer.think_timeout"  # THINKING, 10 s
    AUDIO_PLAYBACK_FINISHED = "audio.playback_finished"
    CONVERSATION_SESSION_LOST = "conversation.session_lost"
    SYSTEM_DEGRADED_EXITED = "system.degraded_exited"


class IllegalTransition(Exception):
    """Raised by :func:`next_state` for a ``(state, trigger)`` pair not in the table.

    Carries both so the message — and any log line built from it — names exactly what was
    attempted from where (SDS §3.10.3).
    """

    def __init__(self, state: RobotState, trigger: Trigger) -> None:
        self.state = state
        self.trigger = trigger
        super().__init__(
            f"illegal transition: no rule for {trigger.name} in state {state.name} "
            f"(SDS §3.10.3)"
        )


# The normative transition table (SDS §3.10.3). The 17 explicit rows are written out so a
# reviewer can diff them against the table directly; guards are noted but NOT evaluated here
# (they belong to the caller — see the module docstring).
_EXPLICIT_TRANSITIONS: dict[tuple[RobotState, Trigger], RobotState] = {
    # guard: all required adapters healthy
    (RobotState.BOOTING, Trigger.SYSTEM_STARTED): RobotState.IDLE,
    (RobotState.IDLE, Trigger.AUDIO_SPEECH_STARTED): RobotState.LISTENING,
    # guard: not quiet hours; §10.4 policy gate passes
    (RobotState.IDLE, Trigger.BEHAVIOR_TRIGGER_FIRED): RobotState.THINKING,
    (RobotState.IDLE, Trigger.PRESENCE_LOST_TIMEOUT): RobotState.SLEEPING,  # +10 min
    (RobotState.SLEEPING, Trigger.VISION_PRESENCE_GAINED): RobotState.IDLE,
    (RobotState.SLEEPING, Trigger.AUDIO_SPEECH_STARTED): RobotState.LISTENING,
    # The turn ends when OUR OWN gate says the user stopped, never when the model's transcript
    # arrives (AVID-158). ``conversation.user_transcribed`` is a *separate, slower* transcription
    # pass: measured on hardware it lands after the assistant's speech-to-speech audio, and
    # sometimes after ``conversation.turn_ended`` (t=52.482 vs t=53.594 in
    # docs/demos/m5_evidence/trace_2026-07-26_streaming.log). Driving this edge from it left
    # AUDIO_PLAYBACK_STARTED arriving in LISTENING — where it is illegal — so SPEAKING was
    # unreachable, the barge-in row below was dead code on hardware, and two bench runs scored
    # zero barge-ins. §3.10.1's diagram has always labelled this edge "turn end detected".
    (RobotState.LISTENING, Trigger.AUDIO_SPEECH_ENDED): RobotState.THINKING,
    (RobotState.LISTENING, Trigger.LISTEN_TIMEOUT): RobotState.IDLE,  # 30 s
    # The user starts a fresh burst before the reply begins — observed twice in the same trace
    # (t=58.986, t=60.171). Without this row the fix above only relocates the wedge from
    # LISTENING to THINKING (AVID-158).
    (RobotState.THINKING, Trigger.AUDIO_SPEECH_STARTED): RobotState.LISTENING,
    (RobotState.THINKING, Trigger.AUDIO_PLAYBACK_STARTED): RobotState.SPEAKING,
    (RobotState.THINKING, Trigger.THINK_TIMEOUT): RobotState.DEGRADED,  # 10 s
    (RobotState.SPEAKING, Trigger.AUDIO_PLAYBACK_FINISHED): RobotState.IDLE,
    # Barge-in — the row that separates a companion from a kiosk; the caller stops playback
    # first (SDS §3.10.3). Speaker.stop() is on the port precisely so this is immediate.
    (RobotState.SPEAKING, Trigger.AUDIO_SPEECH_STARTED): RobotState.LISTENING,
    # --- the overlap: both of them talking at once (AVID-161) ------------------------------
    # This machine has one axis and the robot has two mouths in the room. The model answers an
    # earlier commit while the user has already begun their next utterance — measured at
    # t=61.074 in docs/demos/m5_evidence/trace_2026-07-26_streaming.log, 0.9 s before our own
    # falling edge — and for that window both are speaking. Every assignment below is therefore
    # a compromise; these three are simply the set that leaves no *reachable* illegal
    # transition, which is the only property worth optimising for on one axis. The real answer
    # is two axes, or AVID-163's echo cancellation making the overlap impossible.
    (RobotState.LISTENING, Trigger.AUDIO_PLAYBACK_STARTED): RobotState.SPEAKING,
    # The user stops first. THINKING rather than a SPEAKING self-loop, and that is the whole
    # trick: a self-loop leaves SPEAKING sticky, so the *next* reply's AUDIO_PLAYBACK_STARTED
    # has no row and the wedge merely moves one step later. THINKING is also true — we are
    # waiting on the model for what they just said — and the THINKING rows above pick it up.
    (RobotState.SPEAKING, Trigger.AUDIO_SPEECH_ENDED): RobotState.THINKING,
    # ...and then the overlapping reply drains, with the user already finished.
    (RobotState.THINKING, Trigger.AUDIO_PLAYBACK_FINISHED): RobotState.IDLE,
    # --- recovery rejoins the turn it interrupted (AVID-162) -------------------------------
    # LISTENING, not IDLE. ``_exit_degraded`` has exactly one caller — ConversationService's
    # rising-edge handler, after ``open()`` succeeds — because there is no background
    # reconnect loop (AVID-105 shipped without one). So recovery *always* happens with a turn
    # in flight, and IDLE was never a state the robot was actually in: the recovery turn then
    # drove speech_ended, playback_started and playback_finished from IDLE, all illegal, and
    # the first turn after the robot has been broken came out stateless — no thinking face, no
    # speaking face, no state move behind a barge-in against that reply.
    #
    # LISTENING is the only target that closes *both* continuations. If the user is still
    # talking, their falling edge finds ``LISTENING + speech_ended``. If a slow open outran
    # them — AVID-157 measured opens at 1.5-6.7 s against a much shorter silence_hold_ms, so
    # this is the common case — the reply finds ``LISTENING + playback_started``, the row
    # AVID-161 added for the overlap, doing double duty here. THINKING reads better ("we sent
    # the audio, we are waiting on the model") and dead-ends at once: it has no
    # ``speech_ended`` row.
    #
    # If a background reconnect is ever added it recovers with NO turn in flight, and this row
    # is then wrong for it. That is a new fact and wants its own trigger, not a reused one.
    (RobotState.DEGRADED, Trigger.SYSTEM_DEGRADED_EXITED): RobotState.LISTENING,
}

# The "*any* state + conversation.session_lost -> DEGRADED" row (SDS §3.10.3), generated for
# every state so the frozen table stays the single source of truth. No explicit row uses
# session_lost, so there is no key collision; DEGRADED -> DEGRADED is an intentional
# idempotent self-loop (it too is "any state").
_SESSION_LOST_TRANSITIONS: dict[tuple[RobotState, Trigger], RobotState] = {
    (state, Trigger.CONVERSATION_SESSION_LOST): RobotState.DEGRADED
    for state in RobotState
}

# "DEGRADED + the user's own edges -> DEGRADED" (SDS §3.10.3, AVID-162). One idea rather than
# two rows: **the network machine is dead, the local one is not.** The mic and the VAD gate keep
# running with no session, so the user goes on starting and finishing utterances throughout —
# they are talking to a robot that cannot yet answer. Absorbing those edges says the state is
# unchanged, which is the truth; leaving them out said the same thing via a WARNING apiece,
# which buried the real illegal transitions in noise. Idempotent, exactly like the session_lost
# self-loop above.
#
# The falling edge is not the theoretical half of this pair. AVID-157 measured ``open()`` at
# 1.5-6.7 s against a ``silence_hold_ms`` of a few hundred, so a user who finishes speaking
# before the reopen returns is the *common* case, not the corner one — and if the open fails
# outright, every edge from then on lands here.
#
# The two ``audio.playback_*`` triggers are deliberately NOT in this set: both are driven only
# from ``ConversationService``'s pump, and **every** path into DEGRADED tears that pump down
# before DEGRADED is reachable — ``_on_session_closed`` when the socket drops, and the §6.9
# think timeout (AVID-171) when the model never produces a first token. Both go through the same
# ``_degrade`` helper for exactly this reason: a path that degraded while leaving the pump alive
# would let a late first delta drive ``audio.playback_started`` from DEGRADED, where there is no
# row — and it would strand recovery too, since ``_exit_degraded`` only fires on a reopen. So a
# row for either trigger would be unreachable — and an unreachable row is a lie in a normative
# table. Note ``interrupt`` (which does fire while degraded, cutting whatever the drop
# abandoned) publishes ``audio.playback_finished`` as a *fact* but drives no trigger at all.
_DEGRADED_SPEECH_TRANSITIONS: dict[tuple[RobotState, Trigger], RobotState] = {
    (RobotState.DEGRADED, trigger): RobotState.DEGRADED
    for trigger in (Trigger.AUDIO_SPEECH_STARTED, Trigger.AUDIO_SPEECH_ENDED)
}

TRANSITION_TABLE: Mapping[tuple[RobotState, Trigger], RobotState] = MappingProxyType(
    {
        **_EXPLICIT_TRANSITIONS,
        **_SESSION_LOST_TRANSITIONS,
        **_DEGRADED_SPEECH_TRANSITIONS,
    }
)

# The finite trigger universe the exhaustive test crosses with RobotState (SDS §14.2).
EVENT_TYPES: tuple[Trigger, ...] = tuple(Trigger)


def next_state(current: RobotState, trigger: Trigger) -> RobotState:
    """Return the state ``current`` moves to when ``trigger`` fires (SDS §3.10.3).

    Pure: a total function of its two arguments and the frozen table — no I/O, no clock, no
    globals. Raises :class:`IllegalTransition` for any pair the table does not document.
    """
    try:
        return TRANSITION_TABLE[(current, trigger)]
    except KeyError:
        raise IllegalTransition(current, trigger) from None


# ``StateTransitioned`` lives HERE, not in ``events.py``, and moving it would break the
# build — please read this before "tidying" it (AVID-69).
#
# The event has to name ``RobotState`` and ``Trigger`` to type its payload. If it lived in
# ``events.py``, that module would import this one, creating the chain
# ``affect.py -> events.py -> state.py``. The ``affect-state-orthogonality`` contract in
# ``.importlinter`` is an *independence* contract, and those catch **indirect** chains, not
# just direct imports — so that chain fails CI even though ``affect.py`` never names
# ``state``. Defining the event beside the types it carries keeps ``state.py -> events.py``
# a one-way edge, exactly as ``AffectChanged`` sits in ``affect.py`` for the same reason.
@dataclass(frozen=True, slots=True, kw_only=True)
class StateTransitioned(Event):
    """The operational state machine moved (SDS §9.1.3). Published by ``StateManager``.

    A **fact**, past tense (P4): the transition has already happened and ``to`` is already
    the current state by the time this is on the bus. Subscribers react; nobody vetoes.

    Carries ``trigger`` as the :class:`Trigger` enum rather than its name, so a subscriber
    can ``match`` on it exhaustively under ``mypy --strict`` instead of re-parsing a string
    (the §9.1.3 catalog row was corrected to match — AVID-69). Queue policy is DROP_OLDEST:
    only the latest state is worth acting on.
    """

    name: ClassVar[str] = "state.transitioned"

    from_: RobotState  # trailing underscore: ``from`` is a keyword (SDS §9.1.3)
    to: RobotState  # already current when this publishes
    trigger: Trigger  # what drove it — the §3.10.3 "Event" column
