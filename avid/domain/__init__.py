"""Domain layer — pure logic.

No I/O, no async, no globals, and no third-party imports beyond the stdlib and
``pydantic`` (P1). The domain imports nothing else from ``avid``. Home of the
``Event`` envelope (AVID-6), ``RobotState`` + the transition table (AVID-7),
``Affect`` (AVID-8), the ``audio.*`` events + pre-roll ring buffer (#86), the
degraded-mode cue vocabulary ``Cue`` (AVID-80), and the ``conversation.*`` events +
``TokenUsage`` value (#99).
"""

from avid.domain.affect import Affect, AffectChanged, AffectTier
from avid.domain.audio import (
    AudioPlaybackFinished,
    AudioPlaybackStarted,
    AudioPreRoll,
    AudioSpeechEnded,
    AudioSpeechStarted,
)
from avid.domain.conversation import (
    ConversationAssistantResponded,
    ConversationSessionLost,
    ConversationTurnEnded,
    ConversationTurnStarted,
    ConversationUserTranscribed,
    TokenUsage,
)
from avid.domain.cues import Cue
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
    # conversation.* events + TokenUsage value (#99)
    "ConversationAssistantResponded",
    "ConversationSessionLost",
    "ConversationTurnEnded",
    "ConversationTurnStarted",
    "ConversationUserTranscribed",
    "TokenUsage",
    # Degraded-mode cue bank vocabulary (AVID-80)
    "Cue",
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
