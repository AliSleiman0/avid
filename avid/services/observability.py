"""``ObservabilityService`` — the structured tap on state and behaviour (#242, SDS §3.12.2, §9.1.3).

The §9.1.3 catalog lists an ``Observability`` subscriber against several events. Most of them
already have one: ``conversation.turn_ended`` has the cost meter, the ``conversation.*`` text facts
have ``EpisodeRecorder``. Three did not, and this is them.

**What it does not subscribe to is as much the point as what it does.** ``behavior.proactive_delivered``
and ``behavior.proactive_suppressed`` are already written durably to ``proactive_log`` by
``BehaviorService`` (§10.6), and that table *is* R-08's instrument — the thing §10.6's tuning query
groups over. A second consumer here would produce a second count of the same thing, and when the two
disagreed there would be no way to say which was right. One fact, one instrument.

**Structured JSON, never the SD card.** §3.12.2 is explicit: logs go to the journal, and the Pi's
card is not a log sink. This module formats; where the bytes land is systemd's business.

Counters are plain attributes rather than events, exactly as ``ExpressionService``'s staleness
counters are. They exist to be read by a future ``GET /metrics`` (§9.5) — and, more immediately, so
that "how often did a trigger disable itself?" has an answer that is a number rather than a grep.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import cast

from avid.core.event_bus import DEFAULT_MAXSIZE, Handler, OverflowPolicy, Subscription
from avid.domain import (
    BehaviorTriggerDisabled,
    BehaviorTriggerFired,
    StateTransitioned,
)

_log = logging.getLogger("avid.observability")

_SOURCE = "ObservabilityService"


class ObservabilityService:
    """Log the three uncovered facts as structured JSON, and count them (SDS §3.12.2)."""

    name = _SOURCE

    def __init__(self) -> None:
        # Read by a future GET /metrics (§9.5). Not published as events: a metric is a
        # *question about* the system, and putting it back on the bus would make the bus a
        # subscriber of itself.
        self.transitions = 0
        self.triggers_fired = 0
        self.triggers_disabled = 0

    async def start(self) -> None:
        """Nothing to start. This service owns no task — it is purely reactive."""

    async def stop(self) -> None:
        """Nothing to stop. Idempotent by construction."""

    def subscriptions(self) -> Sequence[Subscription]:
        """The three §9.1.3 rows that had no ``Observability`` consumer.

        ``state.transitioned``'s catalog row tags its Observability subscriber ``(M10)`` — this is
        that tag, discharged. The two ``behavior.*`` rows are the ones the ``proactive_log`` table
        does *not* cover: a trigger firing is not a proposal being considered, and a trigger
        disabling itself is a fact about the *design* rather than about a turn.

        DROP_OLDEST throughout: if the queue ever backs up, the newest transitions are the ones
        worth having, and dropping observability is always preferable to applying backpressure to
        the thing being observed.
        """
        return (
            Subscription(
                event_type=StateTransitioned,
                handler=cast(Handler, self._on_transition),
                name="ObservabilityService.state_transitioned",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=BehaviorTriggerFired,
                handler=cast(Handler, self._on_trigger_fired),
                name="ObservabilityService.trigger_fired",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=BehaviorTriggerDisabled,
                handler=cast(Handler, self._on_trigger_disabled),
                name="ObservabilityService.trigger_disabled",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
        )

    async def _on_transition(self, event: StateTransitioned) -> None:
        self.transitions += 1
        self._emit(
            "state.transitioned",
            event,
            from_=event.from_.name,
            to=event.to.name,
            trigger=event.trigger.value,
        )

    async def _on_trigger_fired(self, event: BehaviorTriggerFired) -> None:
        self.triggers_fired += 1
        self._emit(
            "behavior.trigger_fired",
            event,
            trigger_id=event.trigger_id,
            fact_id=event.fact_id,
        )

    async def _on_trigger_disabled(self, event: BehaviorTriggerDisabled) -> None:
        """§10.5 requires this be *"logged loudly, never silent"* — hence WARNING, not INFO.

        A trigger that turned itself off is diagnostic information about the *design*: it is the
        robot reporting that one of your ideas was bad. That belongs at a level someone reading a
        week of journal will actually see.
        """
        self.triggers_disabled += 1
        self._emit(
            "behavior.trigger_disabled",
            event,
            level=logging.WARNING,
            trigger_id=event.trigger_id,
            ignore_streak=event.ignore_streak,
        )

    def _emit(
        self, name: str, event: object, *, level: int = logging.INFO, **fields: object
    ) -> None:
        """One structured line, with the turn's ``correlation_id`` on it.

        The id is the whole point (§3.12.2): *"one grep on a correlation ID reconstructs the entire
        turn across all seven services"*. A structured log line without it is a fact with no way
        back to the conversation that caused it.
        """
        payload = {
            "event": name,
            "correlation_id": str(getattr(event, "correlation_id", "")),
            **fields,
        }
        _log.log(level, "%s", json.dumps(payload, sort_keys=True))


__all__ = ["ObservabilityService"]
