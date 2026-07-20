"""Services layer — async use-case orchestration.

Each service subscribes to events, calls ports, and publishes events — nothing
else. Depends on ``domain`` and the ``core`` ports; knows nothing concrete and
never imports ``avid.adapters`` (P2, P5).
"""

from avid.services.affect import AffectService
from avid.services.expression import ExpressionService

__all__ = [
    # The affect decider (AVID-71)
    "AffectService",
    # The drawer (AVID-72) — the other half of the SDS §3.6.1 split
    "ExpressionService",
]
