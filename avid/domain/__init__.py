"""Domain layer — pure logic.

No I/O, no async, no globals, and no third-party imports beyond the stdlib and
``pydantic`` (P1). The domain imports nothing else from ``avid``. Home of the
``Event`` envelope (AVID-6), ``RobotState`` + the transition table (AVID-7),
``Affect`` (AVID-8), the ``audio.*`` events + pre-roll ring buffer (#86), the
degraded-mode cue vocabulary ``Cue`` (AVID-80), the ``conversation.*`` events +
``TokenUsage`` value (#99), the ``Fact`` value + ``memory.*`` events + §7.7 scoring
(#116), and ``BBox`` + the ``vision.*`` events (#219), and §10.4's interruption policy — the
pure gate that decides whether the robot may speak first (#235).
"""

from avid.domain.affect import (
    SEMANTIC_AFFECTS,
    Affect,
    AffectChanged,
    AffectTier,
)
from avid.domain.audio import (
    AudioPlaybackFinished,
    AudioPlaybackStarted,
    AudioPreRoll,
    AudioSpeechEnded,
    AudioSpeechStarted,
    EchoFloor,
    HighPass,
    rms_dbfs,
)
from avid.domain.behavior import (
    AMBIENT_SPEECH,
    COOLDOWN,
    DAILY_BUDGET,
    POLICY_RULES,
    PRESENCE,
    PROACTIVE_STATES,
    QUIET_HOURS,
    STATE,
    Delivered,
    PolicyContext,
    PolicyLimits,
    PolicyResult,
    Suppressed,
    TriggerRecord,
    evaluate_policy,
    within_quiet_window,
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
    SystemDegradedEntered,
    SystemDegradedExited,
    SystemHandlerFailed,
    SystemShuttingDown,
    SystemStarted,
    validate_event_name,
)
from avid.domain.memory import (
    FACT_KINDS,
    Fact,
    FactKind,
    MemoryFactDeleted,
    MemoryFactStored,
    MemoryFactSuperseded,
    MemoryRecallCompleted,
    RetrievalCandidate,
    RetrievalMatch,
    ScoredCandidate,
    ScoreWeights,
    deletable_ids,
    rank_candidates,
    recency_decay,
    select_top_facts,
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
from avid.domain.vision import (
    BBox,
    VisionFaceDetected,
    VisionPresenceGained,
    VisionPresenceLost,
)

__all__ = [
    # behavior (#235) — §10.4's interruption policy
    "AMBIENT_SPEECH",
    "COOLDOWN",
    "DAILY_BUDGET",
    "POLICY_RULES",
    "PRESENCE",
    "PROACTIVE_STATES",
    "QUIET_HOURS",
    "STATE",
    "Delivered",
    "PolicyContext",
    "PolicyLimits",
    "PolicyResult",
    "Suppressed",
    "TriggerRecord",
    "evaluate_policy",
    "within_quiet_window",
    # Affect (AVID-8)
    "Affect",
    "AffectChanged",
    "AffectTier",
    "SEMANTIC_AFFECTS",
    # audio.* events + pre-roll ring buffer (#86) + the echo gate's arithmetic (AVID-159)
    "AudioPlaybackFinished",
    "AudioPlaybackStarted",
    "AudioPreRoll",
    "AudioSpeechEnded",
    "AudioSpeechStarted",
    "EchoFloor",
    "HighPass",
    "rms_dbfs",
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
    "SystemDegradedEntered",
    "SystemDegradedExited",
    "SystemHandlerFailed",
    "SystemShuttingDown",
    "SystemStarted",
    "validate_event_name",
    # memory.* events + Fact value + §7.7 scoring (#116)
    "FACT_KINDS",
    "Fact",
    "FactKind",
    "MemoryFactDeleted",
    "MemoryFactStored",
    "MemoryFactSuperseded",
    "MemoryRecallCompleted",
    "RetrievalCandidate",
    "RetrievalMatch",
    "ScoredCandidate",
    "ScoreWeights",
    "deletable_ids",
    "rank_candidates",
    "recency_decay",
    "select_top_facts",
    # RobotState (AVID-7); state.transitioned (AVID-69)
    "EVENT_TYPES",
    "TRANSITION_TABLE",
    "IllegalTransition",
    "RobotState",
    "StateTransitioned",
    "Trigger",
    "next_state",
    # BBox + vision.* events (#219). BBox is re-exported by core/hal.py, which is
    # the spelling ports and adapters use — see that module's docstring for why.
    "BBox",
    "VisionFaceDetected",
    "VisionPresenceGained",
    "VisionPresenceLost",
]
