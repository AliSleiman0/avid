"""Affect policy: state in / face out (AVID-72), and affect in / gesture out (#202).

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

from avid.domain import Affect, Gesture, RobotState

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


# --- affect -> gesture: the HAPPY -> nod arrow (#202, SDS §3.7.2) ------------
#
# The second normative table in this module, and it is here for the same reason the first
# is: it is a **policy** that will be argued about and tuned, and policy in a pure function
# changes with a unit test instead of a bench session. It maps a domain value to another
# domain value and imports nothing device-shaped.
#
# It sits beside ``TIER1`` rather than in ``domain/`` for a weaker reason than that table's
# — nothing here couples ``Affect`` to ``RobotState``, so ``affect-state-orthogonality``
# would not object. It lives here anyway so that the two halves of §3.7.2's fan-out are
# side by side: one affect in, a face out; one affect in, a gesture out. Keeping them apart
# would invite the next reader to put the second one in ``MotionService``, which is where
# the first one started life and had to be moved out of (P5).
GESTURES: Mapping[Affect, Gesture | None] = MappingProxyType(
    {
        # §3.7.2, normative and non-negotiable: "HAPPY -> nod". The arrow that justified the
        # event bus — one publish, the face changes AND the servo nods, and neither
        # subscriber knows the other exists.
        Affect.HAPPY: Gesture.NOD,
        # Dejection is legible on the tilt axis and nowhere else: a head that drops reads as
        # sad to anyone, at any distance, with no context.
        Affect.SAD: Gesture.LOOK_DOWN,
        # A raised head is the pondering pose — "hm?" — and on this rig it is what a "head
        # tilt" can be. ⚠️ The *quizzical* head tilt everyone pictures is a *roll*, and the
        # rig has pan and tilt only (ADR-009); rolling would need a third axis §7.3 does not
        # buy. Looking up is the closest honest reading, not a substitute for one.
        Affect.CONFUSED: Gesture.LOOK_UP,
        # --- and now the ones that map to nothing, which is most of them ----------------
        #
        # **None is the common answer and that is the design.** ``affect.changed`` fires on
        # every state transition, so mapping the Tier-1 four to gestures would have the
        # servos running continuously through a conversation — which fails the gate's
        # "relaxes when idle" clause by construction and puts sustained load on the rail
        # #206 measures. These four are the face's job, not the body's.
        Affect.IDLE: None,
        Affect.LISTENING: None,
        Affect.THINKING: None,
        Affect.SPEAKING: None,
        # SLEEPING is not a still gesture — it is the absence of one. #203 handles sleep by
        # *relaxing*, which is the opposite of moving, and a gesture here would re-energise
        # the servos at the exact moment the robot is supposed to go quiet.
        Affect.SLEEPING: None,
    }
)


def gesture_for(affect: Affect) -> Gesture | None:
    """The gesture *affect* should produce, or ``None`` — which is the usual answer.

    Pure, total over :class:`~avid.domain.Affect`, and indexes directly for the same reason
    :func:`baseline_affect` does: a ninth affect without a mapping must raise
    :class:`KeyError` loudly rather than silently never moving. A robot that quietly stops
    gesturing is indistinguishable from a robot whose servo came unplugged, and the
    exhaustive test over ``Affect`` is what makes that a build failure instead of a bench
    session.

    **This does not couple Affect to RobotState** (§3.10.1, CLAUDE.md §5). It maps affect to
    *gesture* and never consults operational state; ``MotionService`` reads
    ``state.transitioned`` separately for its relax logic, exactly as ``ExpressionService``
    reads it separately for the baseline face, and neither service knows the other does.

    Whether affect *inference* ever fires the interesting affects is §6.8's problem, not
    this table's. Today ``AffectService`` publishes the Tier-1 baseline, which is enough to
    prove the path end to end; when the model starts asking for ``HAPPY``, the nod happens
    with **zero change here**. That is the fan-out working.
    """
    return GESTURES[affect]
