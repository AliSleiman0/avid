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
from enum import Enum, auto
from types import MappingProxyType


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
    """

    SYSTEM_STARTED = "system.started"
    AUDIO_SPEECH_STARTED = "audio.speech_started"
    BEHAVIOR_TRIGGER_FIRED = "behavior.trigger_fired"
    PRESENCE_LOST_TIMEOUT = "timer.presence_lost"  # IDLE, sustained 10 min
    VISION_PRESENCE_GAINED = "vision.presence_gained"
    CONVERSATION_USER_TRANSCRIBED = "conversation.user_transcribed"
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


# The normative transition table (SDS §3.10.3). The 13 explicit rows are written out so a
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
    (RobotState.LISTENING, Trigger.CONVERSATION_USER_TRANSCRIBED): RobotState.THINKING,
    (RobotState.LISTENING, Trigger.LISTEN_TIMEOUT): RobotState.IDLE,  # 30 s
    (RobotState.THINKING, Trigger.AUDIO_PLAYBACK_STARTED): RobotState.SPEAKING,
    (RobotState.THINKING, Trigger.THINK_TIMEOUT): RobotState.DEGRADED,  # 10 s
    (RobotState.SPEAKING, Trigger.AUDIO_PLAYBACK_FINISHED): RobotState.IDLE,
    # Barge-in — the row that separates a companion from a kiosk; the caller stops playback
    # first (SDS §3.10.3). Speaker.stop() is on the port precisely so this is immediate.
    (RobotState.SPEAKING, Trigger.AUDIO_SPEECH_STARTED): RobotState.LISTENING,
    (RobotState.DEGRADED, Trigger.SYSTEM_DEGRADED_EXITED): RobotState.IDLE,
}

# The "*any* state + conversation.session_lost -> DEGRADED" row (SDS §3.10.3), generated for
# every state so the frozen table stays the single source of truth. No explicit row uses
# session_lost, so there is no key collision; DEGRADED -> DEGRADED is an intentional
# idempotent self-loop (it too is "any state").
_SESSION_LOST_TRANSITIONS: dict[tuple[RobotState, Trigger], RobotState] = {
    (state, Trigger.CONVERSATION_SESSION_LOST): RobotState.DEGRADED
    for state in RobotState
}

TRANSITION_TABLE: Mapping[tuple[RobotState, Trigger], RobotState] = MappingProxyType(
    {**_EXPLICIT_TRANSITIONS, **_SESSION_LOST_TRANSITIONS}
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
