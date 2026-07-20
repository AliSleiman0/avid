"""The operational state machine's one owner (AVID-69, SDS §3.8.4, §3.10, §9.1.3).

``RobotState`` lives in the domain as a pure table and a pure :func:`~avid.domain.next_state`;
this is the thin, stateful shell around it. Per SDS §3.8.4 it is **the only shared mutable
object in the process**, it is guarded by an ``asyncio.Lock``, and it is mutated *exclusively*
through :meth:`StateManager.transition`. Services read :attr:`state` and call ``transition``;
none of them keep their own copy, because two copies of the state machine is the bug this
class exists to prevent.

``transition`` is a **direct awaited call, not an event** (SDS §9.1.4): losing a state change
would be a correctness bug, and the bus is explicitly at-most-once notification. The *fact*
that the state moved is what goes on the bus afterwards, as ``state.transitioned``.

Illegal transitions are this module's call to make. The domain raises
:class:`~avid.domain.IllegalTransition` for anything not in the table and deliberately takes no
view on what that means (``domain/state.py``); SDS §3.10.3 sets the production policy —
**logged and ignored, never fatal**. A companion robot that dies because a stray event arrived
in the wrong state is worse than one that shrugs.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from avid.core.envelope import envelope
from avid.core.event_bus import AsyncioEventBus
from avid.core.ports import Clock
from avid.domain import (
    IllegalTransition,
    RobotState,
    StateTransitioned,
    Trigger,
    next_state,
)

_log = logging.getLogger("avid.state")

# The component name stamped on the events this module publishes (SDS §9.1.3).
_SOURCE = "StateManager"


class StateManager:
    """Holds :class:`~avid.domain.RobotState` and publishes every legal move.

    Constructed once, in the composition root (``main.py``), and injected wherever the state
    is needed. *initial* exists for tests that want to start mid-machine; production always
    boots from ``BOOTING`` and reaches ``IDLE`` via the lifecycle's first transition.
    """

    def __init__(
        self,
        *,
        bus: AsyncioEventBus,
        clock: Clock,
        initial: RobotState = RobotState.BOOTING,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._state = initial
        self._lock = asyncio.Lock()

    @property
    def state(self) -> RobotState:
        """The current state. Read freely; write only through :meth:`transition`."""
        return self._state

    async def transition(self, trigger: Trigger, *, correlation_id: UUID) -> RobotState:
        """Apply *trigger*, publish ``state.transitioned``, and return the resulting state.

        Returns the *current* state unchanged — and publishes nothing — if the table has no
        rule for ``(state, trigger)``; the attempt is logged at WARNING with
        *correlation_id* so the turn it belongs to can be reconstructed (SDS §3.12.2).

        *correlation_id* is propagated from whatever caused the transition, never minted
        here: this event is a link in someone else's turn, not the start of one.
        """
        async with self._lock:
            previous = self._state
            try:
                nxt = next_state(previous, trigger)
            except IllegalTransition:
                # SDS §3.10.3: loud in tests (the domain raises), logged-and-ignored here.
                _log.warning(
                    "ignored illegal transition: no rule for %s in state %s "
                    "[correlation_id=%s]",
                    trigger.name,
                    previous.name,
                    correlation_id,
                )
                return previous

            self._state = nxt
            # Published *inside* the lock on purpose: it makes bus order match transition
            # order. Hoisting it out would let two concurrent transitions interleave and
            # tell subscribers the machine moved B->C before A->B.
            await self._bus.publish(
                StateTransitioned(
                    **envelope(
                        clock=self._clock,
                        correlation_id=correlation_id,
                        source=_SOURCE,
                    ),
                    from_=previous,
                    to=nxt,
                    trigger=trigger,
                )
            )
            return nxt
