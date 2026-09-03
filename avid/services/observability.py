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
from collections import Counter
from collections.abc import Sequence
from typing import cast

from avid.core.event_bus import DEFAULT_MAXSIZE, Handler, OverflowPolicy, Subscription
from avid.domain import (
    PREEMPTED,
    BehaviorTriggerDisabled,
    BehaviorTriggerFired,
    DriveStepAborted,
    DriveStepCompleted,
    DriveStepStarted,
    MotionGestureCompleted,
    MotionGesturePreempted,
    MotionGestureStarted,
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
        self.gestures = 0
        # The wheels (#400, SDS §9.5). ``steps_aborted`` is a map keyed by §9.1.3's abort reason
        # rather than a total, for the reason ``illegal_transitions`` is one: an ``edge`` abort
        # and a ``preempted`` abort want opposite responses, and a single number cannot tell
        # them apart. String keys, so the `/metrics` route's ``json.dumps`` never sees an enum.
        self.steps = 0
        self.steps_aborted: Counter[str] = Counter()

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
            Subscription(
                event_type=MotionGestureStarted,
                handler=cast(Handler, self._on_gesture_started),
                name="ObservabilityService.gesture_started",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=MotionGestureCompleted,
                handler=cast(Handler, self._on_gesture_completed),
                name="ObservabilityService.gesture_completed",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=MotionGesturePreempted,
                handler=cast(Handler, self._on_gesture_preempted),
                name="ObservabilityService.gesture_preempted",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
            # The three drive.* rows (#400, ADR-015): as with motion.*, Observability is their
            # only §9.1.3 subscriber, and M12's gate reads the edge abort out of this log.
            Subscription(
                event_type=DriveStepStarted,
                handler=cast(Handler, self._on_step_started),
                name="ObservabilityService.step_started",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=DriveStepCompleted,
                handler=cast(Handler, self._on_step_completed),
                name="ObservabilityService.step_completed",
                policy=OverflowPolicy.DROP_OLDEST,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=DriveStepAborted,
                handler=cast(Handler, self._on_step_aborted),
                name="ObservabilityService.step_aborted",
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

    async def _on_gesture_started(self, event: MotionGestureStarted) -> None:
        """§9.1.3 names Observability as the only subscriber of all three ``motion.*`` rows.

        Without this the events would publish into an empty room — and #207's AC-3 grades
        preemption *"by log **and** by eye"*, which needs a log to read. The axes are named
        because they are the negotiated result: on a rig with no tilt a nod moves ``pan``, and
        that one field is the difference between a real nod and the documented fallback.
        """
        self.gestures += 1
        self._emit(
            "motion.gesture_started",
            event,
            gesture=event.gesture,
            axes=[axis.name for axis in event.axes],
        )

    async def _on_gesture_completed(self, event: MotionGestureCompleted) -> None:
        self._emit(
            "motion.gesture_completed",
            event,
            gesture=event.gesture,
            duration_ms=event.duration_ms,
        )

    async def _on_gesture_preempted(self, event: MotionGesturePreempted) -> None:
        """⚠️ ``by=None`` is §3.12.3's I²C-fault abort, not a missing field — so it is logged at
        WARNING, while an ordinary interruption is INFO.

        The two share a catalog row deliberately, and this is the seam where that decision has
        to be unpicked: a newer gesture cutting an older one is the design working, and a rig
        faulting mid-gesture is hardware trouble. A single level for both would bury the second
        in a week of the first.
        """
        level = logging.INFO if event.by is not None else logging.WARNING
        self._emit(
            "motion.gesture_preempted",
            event,
            level=level,
            gesture=event.gesture,
            by=event.by,
            cause="superseded" if event.by is not None else "fault_abort",
        )

    async def _on_step_started(self, event: DriveStepStarted) -> None:
        """A wheel is about to turn: the heading and the leg length are what a reader needs to
        know what the robot is about to do, without re-deriving them from a plan."""
        self.steps += 1
        self._emit(
            "drive.step_started",
            event,
            gesture=event.gesture,
            heading=event.heading,
            distance_mm=event.distance_mm,
        )

    async def _on_step_completed(self, event: DriveStepCompleted) -> None:
        """``net_mm`` is the plan's own arithmetic and is ``0.0`` for every plan the domain emits —
        logged so a completed step whose payload says otherwise is visibly a defect."""
        self._emit(
            "drive.step_completed",
            event,
            gesture=event.gesture,
            duration_ms=event.duration_ms,
            net_mm=event.net_mm,
        )

    async def _on_step_aborted(self, event: DriveStepAborted) -> None:
        """⚠️ ``edge``, ``fault`` and ``budget`` are WARNING; ``preempted`` is INFO.

        A preemption is the design working — a conversation started mid-step — and it happens
        every time someone speaks during an idle shuffle. The other three are facts about the
        *desk* or the *hardware*: the sensors earning their place, a wheel that faulted, or the
        loud row that should never fire. All three belong at a level someone reading a week of
        journal will actually see, which is the seam §10.5 already drew for ``trigger_disabled``.
        """
        self.steps_aborted[event.reason] += 1
        level = logging.INFO if event.reason == PREEMPTED else logging.WARNING
        self._emit(
            "drive.step_aborted",
            event,
            level=level,
            gesture=event.gesture,
            reason=event.reason,
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
