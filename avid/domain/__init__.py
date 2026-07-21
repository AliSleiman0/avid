"""Domain layer — pure logic.

No I/O, no async, no globals, and no third-party imports beyond the stdlib and
``pydantic`` (P1). The domain imports nothing else from ``avid``. Home of the
``Event`` envelope (AVID-6), ``RobotState`` + the transition table (AVID-7),
``Affect`` (AVID-8), and the ``audio.*`` events + pre-roll ring buffer (#86).
"""

from avid.domain.affect import Affect, AffectChanged, AffectTier
from avid.domain.audio import (
    AudioPlaybackFinished,
    AudioPlaybackStarted,
    AudioPreRoll,
    AudioSpeechEnded,
    AudioSpeechStarted,
)
from avid.domain.events import (
    EVENT_DOMAINS,
    REASON_HANDLER_RAISED,
    REASON_QUEUE_OVERFLOW,
    Event,
    SystemHandlerFailed,
    SystemShuttingDown,
    SystemStarted,
    validate_event_name,
)
from avid.domain.state import (
    EVENT_TYPES,
    TRANSITION_TABLE,
    IllegalTransition,
    RobotState,
    StateTransitioned,
    Trigger,
    next_state,
)

__all__ = [
    # Affect (AVID-8)
    "Affect",
    "AffectChanged",
    "AffectTier",
    # audio.* events + pre-roll ring buffer (#86)
    "AudioPlaybackFinished",
    "AudioPlaybackStarted",
    "AudioPreRoll",
    "AudioSpeechEnded",
    "AudioSpeechStarted",
    # Event envelope (AVID-6)
    "EVENT_DOMAINS",
    "REASON_HANDLER_RAISED",
    "REASON_QUEUE_OVERFLOW",
    "Event",
    "SystemHandlerFailed",
    "SystemShuttingDown",
    "SystemStarted",
    "validate_event_name",
    # RobotState (AVID-7); state.transitioned (AVID-69)
    "EVENT_TYPES",
    "TRANSITION_TABLE",
    "IllegalTransition",
    "RobotState",
    "StateTransitioned",
    "Trigger",
    "next_state",
]
