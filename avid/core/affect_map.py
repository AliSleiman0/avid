"""The Tier-1 map: operational state in, baseline face out (SDS §6.8, AVID-72).

This table is the one place in the project where :class:`~avid.domain.RobotState` meets
:class:`~avid.domain.Affect`, and it lives in ``core/`` for a reason worth stating.

It cannot live in ``domain/``. The ``affect-state-orthogonality`` import contract is an
*independence* contract over ``avid.domain.affect`` <-> ``avid.domain.state``, and it catches
indirect chains: a domain module importing both would couple the two halves §3.10.1 exists to
keep apart. Up here the coupling is explicit, tabulated, and exhaustively tested — which is
the difference between a documented mapping and a combinatorial mess.

It no longer lives in ``services/affect.py`` either, where it started life in AVID-71.
``ExpressionService`` needs the same map to render the Tier-1 face directly, and a service
importing another service is a P5 violation — one the ``service-independence`` contract will
fail on (AVID-73 switched it on, and it passes precisely because of this move). Two copies
would be worse: a normative table with a
second, drifting edition is not normative. So it moved down a layer, where both services can
depend on it without depending on each other.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from avid.domain import Affect, RobotState

# Frozen via MappingProxyType to match ``TRANSITION_TABLE`` (``domain/state.py``): a normative
# table is a normative table wherever it lives.
TIER1: Mapping[RobotState, Affect] = MappingProxyType(
    {
        RobotState.IDLE: Affect.IDLE,
        RobotState.LISTENING: Affect.LISTENING,
        RobotState.THINKING: Affect.THINKING,
        RobotState.SPEAKING: Affect.SPEAKING,
        RobotState.SLEEPING: Affect.SLEEPING,
        # BOOTING has no face of its own — IDLE is the honest baseline for "not yet doing
        # anything", and the boot transition to IDLE then suppresses as a no-op.
        RobotState.BOOTING: Affect.IDLE,
        # DEGRADED stays composed rather than SAD (SDS §6.9). A robot that looks sad about
        # its own outage is telling the user something about the outage, not about them —
        # the degraded *behaviour* communicates the fault; the face should not editorialise.
        RobotState.DEGRADED: Affect.IDLE,
    }
)


def baseline_affect(state: RobotState) -> Affect:
    """The Tier-1 baseline face for an operational state.

    Indexes directly — no ``.get(..., IDLE)`` default. A new :class:`RobotState` without a
    mapping must raise :class:`KeyError` loudly rather than silently wearing the IDLE face
    forever; the exhaustive test over ``RobotState`` is what makes that a build failure
    instead of a 1 a.m. discovery. Both callers sit behind the event bus, which swallows a
    raising handler and republishes ``system.handler_failed``, so even in production this is
    loud rather than fatal.
    """
    return TIER1[state]
