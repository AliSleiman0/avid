"""``BehaviorService`` — the milestone's centre of mass (SDS §10.2, §10.6, #237).

The orchestration §10.2's diagram describes end to end: a scheduler wakes, a trigger *proposes*,
:func:`~avid.domain.behavior.evaluate_policy` decides, and **both outcomes are written down**.

Three things live here and nowhere else.

**Assembling a `PolicyContext`.** Reading the clock, converting to the routine's timezone, aging
the last presence event, folding the ambient-speech window, counting today's deliveries — every one
of those touches the world, which is exactly why none of them is in ``domain/``. The gate stays a
pure function over a value object, and this is the impure half that builds the value.

**Minting the turn's `correlation_id`.** ``behavior.trigger_fired`` is one of exactly two turn
origins (§9.1.1); ``audio.speech_started`` is the other, minted in ``AudioService``. Everything
downstream of a proactive turn carries the id minted here.

**Writing `proactive_log`.** §10.6: *"every considered proposal is logged, delivered or not."* This
is the only instrument R-08 has, and the write is a direct awaited call rather than an event — §4's
rule that the bus carries notifications, not obligations. Losing a suppression would not break the
robot; it would break the only way anyone can ever tell whether the robot is under-firing.

**Subscriptions.** Nine, and the catalog (§9.1.3) is why there are nine rather than the three §10's
prose implies. Shipping only the fire path would leave the service structurally incomplete against
its own catalog entry — the gap M8's epic caught for ``PresenceService`` before it shipped.

⚠️ ``DEGRADED`` gets no rule of its own. It is a ``RobotState``, so rule 2 already vetoes it: a
robot that cannot reach the API has nothing to say unprompted. The ``system.degraded_*``
subscriptions the catalog grants are tracked for diagnostics, not turned into a seventh rule — one
condition, one place, or the two disagree eventually.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from datetime import datetime
from typing import cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from avid.core.envelope import envelope
from avid.core.event_bus import DEFAULT_MAXSIZE, Handler, OverflowPolicy, Subscription
from avid.core.ports import Clock, EventBus, ProactiveLog, TriggerRepository
from avid.core.schedule import InvalidRoutine, next_occurrence
from avid.core.state_manager import StateManager
from avid.domain import (
    AmbientWindow,
    AudioSpeechEnded,
    AudioSpeechStarted,
    BehaviorProactiveDelivered,
    BehaviorProactiveSuppressed,
    BehaviorTriggerFired,
    ConversationUserTranscribed,
    MemoryFactDeleted,
    MemoryFactStored,
    MemoryFactSuperseded,
    PolicyContext,
    PolicyLimits,
    RobotState,
    StateTransitioned,
    Suppressed,
    SystemDegradedEntered,
    SystemDegradedExited,
    SystemStarted,
    Trigger,
    VisionPresenceGained,
    VisionPresenceLost,
    ambient_speech_s,
    attribute_speech,
    evaluate_policy,
    record_speech,
)
from avid.services.scheduler import SchedulerLoop

_log = logging.getLogger("avid.services.behavior")

_SOURCE = "BehaviorService"

#: The outcome strings §8.3's ``CHECK`` accepts.
_DELIVERED = "delivered"
_SUPPRESSED = "suppressed"


class BehaviorService:
    """Decide when the robot should speak first (SDS §3.6.1, §10.2)."""

    name = _SOURCE

    def __init__(
        self,
        *,
        bus: EventBus,
        clock: Clock,
        state: StateManager,
        triggers: TriggerRepository,
        proactive_log: ProactiveLog,
        limits: PolicyLimits,
        timezone: str,
        default_cooldown_s: int,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._state = state
        self._triggers = triggers
        self._log = proactive_log
        self._limits = limits
        self._zone = ZoneInfo(timezone)
        self._default_cooldown_s = default_cooldown_s
        self._scheduler = SchedulerLoop(clock=clock, on_due=self._on_due)

        # --- the world, as the gate needs to see it ---------------------------------------
        # Every one of these mirrors a fact that arrived on the bus. None is read from anywhere
        # else: PresenceService's own filter state is private (P5), and reconstructing presence
        # age from vision.presence_* plus this service's clock is the coupling-free design §9.1.3
        # intended rather than a workaround for it.
        self._robot_state = RobotState.BOOTING
        self._last_present_at: float | None = None  # monotonic seconds
        self._ambient = AmbientWindow()
        self._degraded = False
        # §10.4's manual override — an absolute instant, set by `set_quiet` (#243) and by
        # POST /quiet (#244). One piece of state, two doors.
        self._quiet_until: int | None = None

    # --- SDS §9.2 service shape -----------------------------------------------------------

    async def start(self) -> None:
        """Start the scheduler loop. The heap is rebuilt on ``system.started``, not here.

        Deliberately: the rebuild reads the database, and ``main.py`` starts services before the
        bus, so doing it here would race the store's own first connection. ``system.started`` is
        the fact that says the world is ready, and §9.1.3 already lists this service against it.
        """
        await self._scheduler.start()

    async def stop(self) -> None:
        """Stop the scheduler loop. Idempotent."""
        await self._scheduler.stop()

    def subscriptions(self) -> Sequence[Subscription]:
        """Nine, per §9.1.3 — not the three §10's prose implies.

        Shipping only the fire path would leave this structurally incomplete against its own
        catalog entry. Each name is ``<Service>.<event>`` so §9.1.5's drift check can see it; an
        anonymous handler is invisible to that check, which is why ``name`` is mandatory.

        DROP_OLDEST throughout except the memory facts: for state, presence and speech the *latest*
        reading is the only one worth having, while a lost ``memory.fact_stored`` is a routine that
        never becomes a schedule — so those take DROP_NEWEST, matching §9.1.3's own column.
        """
        oldest = OverflowPolicy.DROP_OLDEST
        newest = OverflowPolicy.DROP_NEWEST
        return (
            Subscription(
                event_type=SystemStarted,
                handler=cast(Handler, self._on_started),
                name="BehaviorService.system_started",
                policy=newest,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=StateTransitioned,
                handler=cast(Handler, self._on_state_transitioned),
                name="BehaviorService.state_transitioned",
                policy=oldest,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=SystemDegradedEntered,
                handler=cast(Handler, self._on_degraded_entered),
                name="BehaviorService.degraded_entered",
                policy=newest,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=SystemDegradedExited,
                handler=cast(Handler, self._on_degraded_exited),
                name="BehaviorService.degraded_exited",
                policy=newest,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=VisionPresenceGained,
                handler=cast(Handler, self._on_presence_gained),
                name="BehaviorService.presence_gained",
                policy=oldest,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=VisionPresenceLost,
                handler=cast(Handler, self._on_presence_lost),
                name="BehaviorService.presence_lost",
                policy=oldest,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=AudioSpeechStarted,
                handler=cast(Handler, self._on_speech_started),
                name="BehaviorService.speech_started",
                policy=oldest,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=AudioSpeechEnded,
                handler=cast(Handler, self._on_speech_ended),
                name="BehaviorService.speech_ended",
                policy=oldest,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=ConversationUserTranscribed,
                handler=cast(Handler, self._on_user_transcribed),
                name="BehaviorService.user_transcribed",
                policy=oldest,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=MemoryFactStored,
                handler=cast(Handler, self._on_fact_stored),
                name="BehaviorService.fact_stored",
                policy=newest,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=MemoryFactSuperseded,
                handler=cast(Handler, self._on_fact_superseded),
                name="BehaviorService.fact_superseded",
                policy=newest,
                maxsize=DEFAULT_MAXSIZE,
            ),
            Subscription(
                event_type=MemoryFactDeleted,
                handler=cast(Handler, self._on_fact_deleted),
                name="BehaviorService.fact_deleted",
                policy=newest,
                maxsize=DEFAULT_MAXSIZE,
            ),
        )

    # --- the world, arriving ----------------------------------------------------------------

    async def _on_started(self, event: SystemStarted) -> None:
        """Rebuild the scheduler's heap from persisted triggers (§10.3, AC-3).

        This is what makes M7's *"restart the process, recall all 20"* true of schedules as well as
        facts: everything that does not survive a restart is a cache, and a schedule the user gave
        us last week is not a cache.
        """
        restored = 0
        for record in await self._triggers.enabled_triggers():
            if record.next_fire_at is not None:
                self._scheduler.schedule(record.id, fire_at=record.next_fire_at)
                restored += 1
        _log.info(
            "rebuilt %d trigger(s) from the store [correlation_id=%s]",
            restored,
            event.correlation_id,
        )

    async def _on_state_transitioned(self, event: StateTransitioned) -> None:
        """Track the operational state rule 2 reads. The state machine is the authority; this is a
        mirror of its last conclusion, never a second opinion."""
        self._robot_state = event.to

    async def _on_degraded_entered(self, event: SystemDegradedEntered) -> None:
        self._degraded = True

    async def _on_degraded_exited(self, event: SystemDegradedExited) -> None:
        self._degraded = False

    async def _on_presence_gained(self, event: VisionPresenceGained) -> None:
        """Rule 3's clock starts here. ``PresenceService`` holds its own filter state privately
        (P5), so presence age is reconstructed from the decisions it publishes plus this service's
        own clock — which is the decoupling §9.1.3 intended, not a workaround for it."""
        self._last_present_at = self._monotonic_s()

    async def _on_presence_lost(self, event: VisionPresenceLost) -> None:
        """Presence *ends*, and the last sighting is what rule 3 ages from — so the mark is set to
        when they were actually last seen, not to now. ``absent_for_s`` is measured from the last
        positive detection (§9.1.3), which is exactly that instant."""
        self._last_present_at = self._monotonic_s() - event.absent_for_s

    async def _on_speech_started(self, event: AudioSpeechStarted) -> None:
        """Reserved for §10.5's reply detection (#241): speech inside the hold-open window is the
        user answering a proactive turn. Tracked here now so the subscription is registered once
        and the catalog stays honest; the backoff that reads it lands with #241."""

    async def _on_speech_ended(self, event: AudioSpeechEnded) -> None:
        """Rule 4's raw feed: every utterance the VAD heard, counted until something retracts it."""
        self._ambient = record_speech(
            self._ambient,
            correlation_id=event.correlation_id,
            at_s=self._monotonic_s(),
            duration_s=event.duration_ms / 1000.0,
            keep_s=self._limits.presence_window_s * 2,
        )

    async def _on_user_transcribed(self, event: ConversationUserTranscribed) -> None:
        """Rule 4's retraction: this turn *was* directed at the robot, so it is not ambient."""
        self._ambient = attribute_speech(self._ambient, event.correlation_id)

    # --- trigger lifecycle (AC-2) -----------------------------------------------------------

    async def _on_fact_stored(self, event: MemoryFactStored) -> None:
        """A stored routine becomes a live schedule — how UC-02 becomes UC-03 (§3.7.3).

        ``MemoryService`` has no idea this service exists; it published a fact. Filtering on
        ``kind`` here rather than asking the store first is why the payload carries it (§9.1.3).
        """
        if event.kind != "routine":
            return
        await self._register(event.fact_id, correlation_id=event.correlation_id)

    async def _on_fact_superseded(self, event: MemoryFactSuperseded) -> None:
        """§7.8's correction: coffee at 08:00 becomes 08:30. The old fact's trigger goes and the
        new fact's is registered — an *edit*, not a second reminder every morning."""
        await self._unregister(event.old_id)
        await self._register(event.new_id, correlation_id=event.correlation_id)

    async def _on_fact_deleted(self, event: MemoryFactDeleted) -> None:
        """UC-07's hard delete. A schedule the user withdrew consent for must not go off — a
        privacy property, not a convenience one."""
        await self._unregister(event.fact_id)

    async def _register(self, fact_id: int, *, correlation_id: UUID) -> None:
        """Resolve a routine fact's next occurrence and put it in the heap.

        A routine fact with no ``routines`` row is a normal outcome — not every routine has a clock
        time — but it is logged, because it is otherwise indistinguishable from a model that has
        stopped filling ``remember_fact``'s ``schedule`` (§6.6, and #310's shape exactly).
        """
        routine = await self._triggers.routine_for(fact_id)
        if routine is None:
            _log.info(
                "fact %d is a routine with no schedule — nothing to fire [correlation_id=%s]",
                fact_id,
                correlation_id,
            )
            return
        try:
            fire_at = next_occurrence(routine, after=self._clock.now())
        except InvalidRoutine:
            # Should be unreachable: MemoryService resolves the schedule before writing it
            # (#314). If it happens anyway the row is bad, and a trigger that cannot be scheduled
            # must say so rather than sit in the database looking scheduled.
            _log.exception(
                "fact %d has an unusable routine; not scheduling [correlation_id=%s]",
                fact_id,
                correlation_id,
            )
            return
        trigger_id = await self._triggers.upsert_routine_trigger(
            fact_id,
            next_fire_at=fire_at,
            cooldown_s=self._default_cooldown_s,
            at=self._clock.now(),
        )
        if fire_at is None:
            self._scheduler.cancel(trigger_id)
            return
        self._scheduler.schedule(trigger_id, fire_at=fire_at)

    async def _unregister(self, fact_id: int) -> None:
        """Drop a fact's trigger from both the heap and the store."""
        for record in await self._triggers.enabled_triggers():
            if record.fact_id == fact_id:
                self._scheduler.cancel(record.id)
        await self._triggers.remove_for_fact(fact_id)

    # --- the decision (AC-5/AC-6/AC-7) ------------------------------------------------------

    async def _on_due(self, trigger_id: int) -> None:
        """A trigger came due: build the context, ask the gate, and write the row either way."""
        record = await self._triggers.get(trigger_id)
        if record is None or not record.enabled:
            return  # deleted or disabled since the heap was built

        now = self._clock.now()
        context = await self._context(
            now=now,
            trigger_last_fired_s=(
                math.inf
                if record.last_fired_at is None
                else float(now - record.last_fired_at)
            ),
            trigger_cooldown_s=float(record.cooldown_s),
        )
        verdict = evaluate_policy(context, limits=self._limits)

        if isinstance(verdict, Suppressed):
            await self._suppress(record.id, rule=verdict.rule, at=now)
            return
        await self._deliver(record.id, fact_id=record.fact_id, at=now)

    async def _suppress(self, trigger_id: int, *, rule: str, at: int) -> None:
        """Log the veto and say so on the bus — never an early return.

        §10.6: *"without this table, [under-firing and never-firing] look identical from the
        outside"*. The row is the durable half and is written first; the event is the same fact,
        told live.
        """
        await self._log.record(
            trigger_id=trigger_id,
            considered_at=at,
            outcome=_SUPPRESSED,
            reason=rule,
            utterance=None,
        )
        await self._bus.publish(
            BehaviorProactiveSuppressed(
                **envelope(clock=self._clock, correlation_id=uuid4(), source=_SOURCE),
                trigger_id=trigger_id,
                rule=rule,
            )
        )
        _log.info("proactive proposal suppressed by %s (trigger %d)", rule, trigger_id)

    async def _deliver(self, trigger_id: int, *, fact_id: int | None, at: int) -> None:
        """Mint the turn, publish the origin, and drive the state machine.

        The ``correlation_id`` is minted **here** — this is the head of the turn (§9.1.1), the only
        other minting site in the system besides ``AudioService``'s falling edge.

        ``state.transition`` is called directly rather than via the bus, exactly as ``AudioService``
        and ``PresenceService`` do at their own edges: ``StateManager`` declares no subscriptions,
        and the publisher of a fact is always the caller of ``transition()``. An illegal transition
        is logged and ignored there — the gate is the first line of defence and rule 2 has already
        checked, so reaching that guard means two things disagreed and the state table wins.
        """
        correlation_id = uuid4()
        await self._triggers.record_fired(
            trigger_id, at=at, next_fire_at=await self._next_fire_for(fact_id)
        )
        await self._log.record(
            trigger_id=trigger_id,
            considered_at=at,
            outcome=_DELIVERED,
            reason=None,
            utterance=None,  # §10.8 composes the words once the model is on the line
        )
        await self._bus.publish(
            BehaviorTriggerFired(
                **envelope(
                    clock=self._clock, correlation_id=correlation_id, source=_SOURCE
                ),
                trigger_id=trigger_id,
                fact_id=fact_id,
            )
        )
        await self._bus.publish(
            BehaviorProactiveDelivered(
                **envelope(
                    clock=self._clock, correlation_id=correlation_id, source=_SOURCE
                ),
                trigger_id=trigger_id,
                utterance="",  # filled by #240's context block once the turn speaks
            )
        )
        await self._state.transition(
            Trigger.BEHAVIOR_TRIGGER_FIRED, correlation_id=correlation_id
        )

    async def _next_fire_for(self, fact_id: int | None) -> int | None:
        """The occurrence after this one, or ``None`` when the rule has run out.

        Re-resolved from the routine rather than incremented, because "the next one" across a DST
        boundary is not "this one plus 86400" — which is the entire argument of §10.3.
        """
        if fact_id is None:
            return None
        routine = await self._triggers.routine_for(fact_id)
        if routine is None:
            return None
        try:
            return next_occurrence(routine, after=self._clock.now())
        except InvalidRoutine:
            _log.exception("cannot re-arm fact %d; leaving it unscheduled", fact_id)
            return None

    # --- context assembly -------------------------------------------------------------------

    async def _context(
        self, *, now: int, trigger_last_fired_s: float, trigger_cooldown_s: float
    ) -> PolicyContext:
        """Everything §10.4's six rules read, gathered from the world at this instant."""
        last_delivered = await self._log.last_delivered_at()
        return PolicyContext(
            now=now,
            local_minutes=self._local_minutes(now),
            state=self._robot_state,
            presence_age_s=(
                math.inf
                if self._last_present_at is None
                else self._monotonic_s() - self._last_present_at
            ),
            ambient_speech_s=ambient_speech_s(
                self._ambient,
                now_s=self._monotonic_s(),
                window_s=self._limits.presence_window_s,
            ),
            last_proactive_s=(
                math.inf if last_delivered is None else float(now - last_delivered)
            ),
            delivered_today=await self._log.delivered_since(
                since=self._local_day_start(now)
            ),
            trigger_last_fired_s=trigger_last_fired_s,
            trigger_cooldown_s=trigger_cooldown_s,
            quiet_until=self._quiet_until,
        )

    def _local_minutes(self, now: int) -> int:
        """Minutes since local midnight, in ``[behavior] timezone``.

        The conversion lives here rather than in the gate because resolving an IANA zone is neither
        pure nor available in ``domain/`` (§10.3). The gate compares numbers; this reads the world.
        """
        local = datetime.fromtimestamp(now, tz=self._zone)
        return local.hour * 60 + local.minute

    def _local_day_start(self, now: int) -> int:
        """Midnight **local**, as a UTC epoch second — rule 6's "today".

        Not ``now - 86400``: a rolling 24 hours would let five deliveries at 23:00 silence the
        whole of the next morning, which is the one part of the day proactivity exists for. And not
        UTC midnight, which in Asia/Beirut resets the budget at 02:00 or 03:00 depending on the
        season.
        """
        local = datetime.fromtimestamp(now, tz=self._zone)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(midnight.timestamp())

    def _monotonic_s(self) -> float:
        """Monotonic seconds. Ages are computed from this, never from wall clock — an NTP step
        would otherwise make presence look hours old and veto everything until the next sighting
        (CLAUDE.md §4: never subtract ``timestamp_ms``)."""
        return self._clock.monotonic_ns() / 1_000_000_000


__all__ = ["BehaviorService"]
