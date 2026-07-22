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
from avid.services.expression import ExpressionService

__all__ = [
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
    # The drawer (AVID-72) — the other half of the SDS §3.6.1 split
    "ExpressionService",
]
