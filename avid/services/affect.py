"""The affect decider — SDS §6.8's two tiers, blended into one fact (AVID-71).

``avid/domain/affect.py`` is deliberately pure data and says so: the blending that decides
*which* affect is current belongs to a service, not the domain. This is that service, and it
**decides only** (SDS §3.6.1). It has never heard of a display, a servo, or a face — it
publishes one ``affect.changed`` and the fan-out is somebody else's problem. That separation
is the whole point of §3.5.1's bus: ``set_affect`` arrives, one event publishes, the face
changes *and* the servo nods, and nothing here learned that either exists.

**The two speeds (SDS §6.8).** Tier 1 is derived locally from ``RobotState`` transitions and
lands in under 20 ms — the face is *already correct* before the model has an opinion. Tier 2
is the model's ``set_affect`` semantic overlay, ~400 ms away, and its latency is invisible
precisely because the Tier-1 baseline is never wrong. The blend is one line:

    blended = overlay if overlay is not None else baseline

A Tier-2 overlay persists until the next Tier-1 change, at which point it is **cleared**: the
operational state has moved, so the model's semantic read of the *previous* situation is
stale. That clearing is what stops the robot grinning through "I've finished speaking."
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import cast
from uuid import UUID

from avid.core.affect_map import baseline_affect
from avid.core.envelope import envelope
from avid.core.event_bus import (
    DEFAULT_MAXSIZE,
    Handler,
    OverflowPolicy,
    Subscription,
)
from avid.core.ports import Clock, EventBus
from avid.domain import (
    Affect,
    AffectChanged,
    AffectTier,
    StateTransitioned,
)

# The component name stamped on the events this module publishes (SDS §9.1.3).
_SOURCE = "AffectService"


class AffectService:
    """Blends SDS §6.8's two affect tiers and publishes a single ``affect.changed``.

    Shaped to SDS §9.2 (``name`` / ``start`` / ``stop`` / ``subscriptions``) without yet
    naming a ``Service`` Protocol — a Protocol with one implementer is a guess about the
    second. Structural typing means #73 can declare it once ``ExpressionService`` exists and
    both satisfy it with no edit here.

    Depends on the :class:`~avid.core.ports.EventBus` and :class:`~avid.core.ports.Clock`
    **Protocols**, never a concrete adapter (P2). Constructed once, in the composition root.
    """

    name = _SOURCE

    def __init__(self, *, bus: EventBus, clock: Clock) -> None:
        self._bus = bus
        self._clock = clock
        # Tier 1: the operational baseline. IDLE is right at construction — the machine boots
        # in BOOTING, which maps to IDLE anyway.
        self._baseline: Affect = Affect.IDLE
        # Tier 2: the model's semantic overlay, or None for "no opinion yet". `None` rather
        # than a sentinel Affect because "no overlay" is genuinely a different thing from
        # "overlay happens to equal the baseline" — only the former is cleared for free.
        self._overlay: Affect | None = None
        # What was last *published*. Compared against, so AC-4 suppression is about what
        # subscribers have actually been told, not about internal bookkeeping.
        self._current: Affect = Affect.IDLE
        # Mutated from two directions — the bus handler and a direct set_affect() call — so
        # it is guarded, exactly as StateManager guards RobotState (SDS §3.8.4).
        self._lock = asyncio.Lock()

    # --- SDS §9.2 service shape ----------------------------------------------------------

    async def start(self) -> None:
        """No owned tasks: this service is purely reactive. Present for the §9.2 shape."""

    async def stop(self) -> None:
        """Idempotent and instant — there is nothing to unwind (§9.2's 5 s budget)."""

    def subscriptions(self) -> Sequence[Subscription]:
        """Declare, do not register (SDS §9.2).

        The service says what it wants; ``main.py`` registers it before ``bus.start()``. That
        inversion is what keeps the subscriber graph static and knowable for the §9.1.5 drift
        check — and it is why this method builds :class:`Subscription` values rather than
        calling ``bus.subscribe`` itself.

        DROP_OLDEST because the latest affect is the only one worth rendering (SDS §9.1.3):
        a backlog of stale faces is worse than no backlog.
        """
        return (
            Subscription(
                event_type=StateTransitioned,
                # Same bridge ``AsyncioEventBus.subscribe`` makes: a handler typed on its
                # own event is safe to store as the base ``Handler``, because dispatch is by
                # exact runtime type and only ever hands it a ``StateTransitioned``.
                handler=cast(Handler, self._on_state_transitioned),
                name="AffectService.state_transitioned",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
        )

    # --- the blend -----------------------------------------------------------------------

    @property
    def affect(self) -> Affect:
        """The current blended affect. Read freely; changed only through the two paths below."""
        return self._current

    async def _on_state_transitioned(self, event: StateTransitioned) -> None:
        """Tier 1: the operational state moved, so the baseline moves and the overlay dies.

        The table itself lives in ``core/affect_map.py`` rather than here: ``ExpressionService``
        needs the same mapping to render the Tier-1 face directly, and one service importing
        another is a P5 violation (AVID-72). See that module for the full reasoning.
        """
        async with self._lock:
            self._baseline = baseline_affect(event.to)
            self._overlay = None
            await self._publish(tier=1, correlation_id=event.correlation_id)

    async def set_affect(self, affect: Affect, *, correlation_id: UUID) -> None:
        """Tier 2: the model's semantic overlay (SDS §6.8).

        A **direct awaited call, not an event** — the same reasoning as
        ``StateManager.transition`` (SDS §9.1.4): the caller is the tool handler acting on the
        model's request, and the bus is at-most-once notification. The *fact* that the affect
        changed is what goes on the bus, afterwards.

        *correlation_id* is required and propagated from the turn the tool call belongs to,
        never minted here (SDS §9.1.1). The AC writes this as ``set_affect(affect)``; there is
        no triggering event in scope to read an id from, and minting one would orphan every
        Tier-2 event from its turn — defeating the single grep the whole correlation scheme
        exists to enable.
        """
        async with self._lock:
            self._overlay = affect
            await self._publish(tier=2, correlation_id=correlation_id)

    async def _publish(self, *, tier: AffectTier, correlation_id: UUID) -> None:
        """Publish ``affect.changed`` if the blend actually moved. Call under the lock.

        Publishing **inside** the lock is deliberate, and for the same reason
        ``StateManager`` does it: it makes bus order match decision order. Hoisting it out
        would let two concurrent changes interleave and tell subscribers the affect went
        HAPPY->IDLE before IDLE->HAPPY.

        The no-op guard is not micro-optimisation. Queues are DROP_OLDEST, so a redundant
        publish can evict a real one, and every redundant event downstream is a wasted render.
        """
        blended = self._overlay if self._overlay is not None else self._baseline
        if blended is self._current:
            return
        previous, self._current = self._current, blended
        await self._bus.publish(
            AffectChanged(
                **envelope(
                    clock=self._clock,
                    correlation_id=correlation_id,
                    source=_SOURCE,
                ),
                affect=blended,
                tier=tier,
                previous=previous,
            )
        )
