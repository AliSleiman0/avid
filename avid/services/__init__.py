"""Services layer — async use-case orchestration.

Each service subscribes to events, calls ports, and publishes events — nothing
else. Depends on ``domain`` and the ``core`` ports; knows nothing concrete and
never imports ``avid.adapters`` (P2, P5).
"""

from avid.services.affect import AffectService
from avid.services.audio import AudioService
from avid.services.conversation import ConversationService
from avid.services.cost_meter import CostMeterService
from avid.services.cue_bank import CUE_FILES, CueBank
from avid.services.episode_recorder import EpisodeRecorder
from avid.services.expression import ExpressionService
from avid.services.memory import MemoryService
from avid.services.presence import PresenceService
from avid.services.tools import CAPABILITY_INSTRUCTIONS, TOOL_SCHEMAS

__all__ = [
    # The §7.6 capability instruction text + the §6.6 tool declarations (#125), seeded into the
    # session by the composition root
    "CAPABILITY_INSTRUCTIONS",
    "TOOL_SCHEMAS",
    # cue→file manifest for the degraded WAV bank (AVID-80)
    "CUE_FILES",
    # The affect decider (AVID-71)
    "AffectService",
    # The audio loop — VAD gate, pre-roll, audio.* facts (AVID-79)
    "AudioService",
    # The conversation loop — Realtime session, conversation.* + degraded facts (#102)
    "ConversationService",
    # The cost meter — §6.10.6 mandatory instrumentation, projected monthly spend (#105)
    "CostMeterService",
    # The degraded-mode WAV cue bank (AVID-80)
    "CueBank",
    # The write-only §7.5 transcript observer + 90-day prune (#123)
    "EpisodeRecorder",
    # The drawer (AVID-72) — the other half of the SDS §3.6.1 split
    "ExpressionService",
    # The sole writer/reader of persistent memory — §9.1.4 direct-call surface (#122)
    "MemoryService",
    # The camera loop — the only clock-driven service; publishes vision.* decisions (#223)
    "PresenceService",
]
