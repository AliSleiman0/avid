"""``Affect`` and the ``affect.changed`` event (AVID-8, SDS §3.10.1, §6.8, §9.1.3).

The robot's **emotional** state. ``Affect`` is *not* the operational state machine — that
is ``RobotState`` (AVID-7) — and the two are deliberately **orthogonal** (SDS §3.10.1): the
robot can be SLEEPING-and-content or LISTENING-and-confused. Nothing in this module may
couple them; conflating them is the most common design error in this class of project and
it produces a combinatorial mess you can't refactor out of later. The
``affect-state-orthogonality`` import-linter contract makes that rule mechanical — this
module must never import ``avid.domain.state``.

This module is pure data: the enum and the event envelope only. The two-tier *blending*
that decides which affect is current (SDS §6.8) is ``AffectService``'s job (a service, M3),
not the domain's.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import ClassVar, Literal, TypeAlias

from avid.domain.events import Event

# The two-speed affect model (SDS §6.8): Tier 1 is derived locally from ``RobotState``
# transitions (<20 ms — the face is already correct before the model has an opinion);
# Tier 2 is the model's ``set_affect`` semantic overlay (~400 ms, and its latency is
# invisible because Tier 1 is never wrong). A subscriber may use ``tier`` to decide
# whether a change should animate or snap.
AffectTier: TypeAlias = Literal[1, 2]


class Affect(Enum):
    """The robot's emotional state (SDS §3.10.1, §6.8). Orthogonal to ``RobotState``.

    Members split across §6.8's two tiers: IDLE / LISTENING / THINKING / SPEAKING are the
    operational-baseline faces (Tier 1); HAPPY / SAD / CONFUSED are the semantic overlays
    the model asks for (Tier 2); SLEEPING is the presence-lost rest face.
    """

    IDLE = auto()
    LISTENING = auto()
    THINKING = auto()
    SPEAKING = auto()
    HAPPY = auto()
    SAD = auto()
    CONFUSED = auto()
    SLEEPING = auto()


@dataclass(frozen=True, slots=True, kw_only=True)
class AffectChanged(Event):
    """The blended affect changed (SDS §9.1.3). Published by ``AffectService``; the fan-out
    that justified the bus (§3.5.1) — one publish, the face changes *and* the servo nods,
    and ``AffectService`` has never heard of either.

    ``tier`` distinguishes §6.8's two speeds; ``previous`` lets a subscriber animate the
    transition. Queue policy is DROP_OLDEST (§9.1.3): the latest affect wins, stale frames
    are worthless.
    """

    name: ClassVar[str] = "affect.changed"

    affect: Affect  # the new blended affect
    tier: AffectTier  # 1 = local RobotState-derived; 2 = model set_affect (§6.8)
    previous: Affect  # what it changed from — for animating the transition
