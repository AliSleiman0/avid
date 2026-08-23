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

:meth:`StateManager.watch` exists for the same reason, and only for that reason (#452). Nothing
here moves the machine on its own — it still has no timer and no clock of its own — but a
subscriber that must arm or disarm something on *every* entry into a state cannot ride
``state.transitioned``: that subscription is at-most-once behind a bounded DROP_OLDEST queue, so
one overflow silently drops the arming and the invariant it protects. An observer registered
here is called by direct, synchronous invocation inside the same lock that made the move.

Illegal transitions are this module's call to make. The domain raises
:class:`~avid.domain.IllegalTransition` for anything not in the table and deliberately takes no
view on what that means (``domain/state.py``); SDS §3.10.3 sets the production policy —
**logged and ignored, never fatal**. A companion robot that dies because a stray event arrived
in the wrong state is worse than one that shrugs.

They are also **counted**, per ``(state, trigger)`` pair, and that is the only thing about them
anything outside this process can see (#456) — see :meth:`StateManager.illegal_transitions`.
Shrugging is still the policy; being unable to find out afterwards was not part of it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol
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


class TransitionObserver(Protocol):
    """A synchronous reaction to every legal move, registered with :meth:`StateManager.watch`.

    Deliberately **not** in ``core/ports.py``: this is not a device port, so it carries none of
    the §14.4 contract-suite or P6 fake obligations — it is a seam on the one object services
    already hold, for the one thing the bus cannot carry (see the module docstring).

    ⚠️ **Synchronous, and that is the whole design.** An observer cannot await, so it can neither
    deadlock on the manager's lock nor re-enter :meth:`StateManager.transition`, and it runs to
    completion between two awaits — which is also why an observer may touch a service's own task
    handles without taking that service's lock. It runs **on the event loop, inside the lock**:
    arm or cancel a task and return (P8). Any I/O here stalls every transition in the process.
    """

    def __call__(
        self,
        *,
        from_: RobotState,
        to: RobotState,
        trigger: Trigger,
        correlation_id: UUID,
    ) -> None: ...


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
        self._observers: list[tuple[str, TransitionObserver]] = []
        # Rejected transitions, per `(state, trigger)` pair. See :meth:`illegal_transitions`.
        self._rejected: dict[str, int] = {}

    @property
    def state(self) -> RobotState:
        """The current state. Read freely; write only through :meth:`transition`."""
        return self._state

    def illegal_transitions(self) -> dict[str, int]:
        """How many times each ``(state, trigger)`` pair has been rejected (#456, SDS §3.12.2).

        **The signal that was there for a day and nobody could read.** An illegal transition is
        logged and ignored (§3.10.3) and publishes nothing, so until now the only reader was a
        human tailing journald — and on the M11 rig that meant a wedge announced itself **147
        times in 175 log lines** while `/health`, `/metrics` and every graded soak criterion
        reported a healthy robot.

        ⚠️ **Per pair, not a total, and that is the whole of it.** A single counter cannot
        distinguish a wedge from ordinary noise, because *some* rejections are expected and
        documented: ``PresenceService`` drives ``VISION_PRESENCE_GAINED`` unconditionally by
        design (#224), so an awake robot logs one every time somebody sits down. The shape
        separates them without a threshold — the rig's own boot reads
        ``{"THINKING/PRESENCE_LOST_TIMEOUT": 135, "THINKING/VISION_PRESENCE_GAINED": 17,
        "IDLE/VISION_PRESENCE_GAINED": 1}``, where the wedge is the number that kept climbing and
        the benign one is the number that did not.

        ⚠️ **Pre-rendered string keys, deliberately.** A tuple or enum key raises ``TypeError`` in
        the ``json.dumps`` behind ``GET /metrics`` and takes the **whole endpoint** down with a
        500 — one unreadable metric would cost every other one. ``bus_queues`` is the precedent
        for the shape.

        Growth is bounded by ``|RobotState| x |Trigger|`` (7 x 12 = 84 keys), so this needs no cap
        and no eviction — the usual objection to an unbounded counter map does not apply.

        A **copy**, so a reader cannot mutate the manager's own tally, and cheap and synchronous
        so the metrics snapshot may call it inline while the robot is mid-turn (P8). An empty map
        means *nothing has been rejected*, which is a real reading and not an absent one.
        """
        return dict(self._rejected)

    def watch(self, observer: TransitionObserver, *, name: str) -> None:
        """Register *observer* to be called on every legal move (#452).

        Composition-time only, from ``main.py`` — like ``subscriptions()``, and for the same
        reason. *name* is **mandatory**, exactly as it is on ``EventBus.subscribe``: an anonymous
        observer is invisible to anyone reading the wiring, and this one runs inside the lock
        that owns the only shared mutable object in the process.
        """
        self._observers.append((name, observer))

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
                # Counted before it is logged (#456). The log line is for a human who happens to
                # be reading; this is for everything else — `GET /metrics`, the soak sampler, and
                # anyone asking the question thirty days later. Same policy either way: SDS
                # §3.10.3 says logged-and-ignored, and counting is not reacting.
                pair = f"{previous.name}/{trigger.name}"
                self._rejected[pair] = self._rejected.get(pair, 0) + 1
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
            # Observers last, and still inside the lock (#452). Last, because they react to a
            # move that has already happened — the same tense the bus fact is in. Inside, because
            # the point of this seam is that an arming cannot be lost between the move and the
            # reaction; hoisting it out would reintroduce the window the bus already has.
            for name, observer in self._observers:
                try:
                    observer(
                        from_=previous,
                        to=nxt,
                        trigger=trigger,
                        correlation_id=correlation_id,
                    )
                except Exception:  # noqa: BLE001 - an observer bug must not lose the move
                    # The one thing this class exists never to lose is the transition itself, and
                    # it is already applied and published by here. So this is the bus's own
                    # swallow-and-log policy (SDS §3.5), for the same reason: a broken reaction
                    # must not take down the machine it was only watching.
                    _log.exception(
                        "state observer %s raised on %s -> %s [correlation_id=%s]",
                        name,
                        previous.name,
                        nxt.name,
                        correlation_id,
                    )
            return nxt
