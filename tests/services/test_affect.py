"""The two-tier affect blend (AVID-71, SDS §6.8).

Real :class:`AsyncioEventBus`, real :class:`FakeClock`, no mocks — ``unittest.mock`` is banned
outside ``tests/adapters/`` (SDS §14.3), and here it would be actively worse: the properties
under test are *about* dispatch and envelope stamping, which a mock would assert away rather
than exercise.

The collector below waits on an :class:`asyncio.Event`, never a sleep. The bus **cancels** its
workers on ``stop()`` rather than draining them, so "publish then sleep a bit" is a race that
passes locally and fails on a loaded CI box.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import NamedTuple
from uuid import UUID, uuid4

import pytest

from avid.adapters.clock import FakeClock
from avid.core.event_bus import AsyncioEventBus
from avid.domain import Affect, AffectChanged, RobotState, StateTransitioned, Trigger
from avid.services.affect import _TIER1, AffectService

# A publish reaches its subscriber in a couple of scheduler turns; a whole second is a
# generous ceiling that still fails fast if the bus ever wedges.
_ARRIVAL_TIMEOUT_S = 1.0


class _Collector:
    """Records every ``affect.changed`` and lets a test await the *n*-th one.

    Deliberately not a mock: it is a real subscriber on a real bus, so what it records is
    what a real subscriber would have been handed.
    """

    def __init__(self) -> None:
        self.events: list[AffectChanged] = []
        self._arrived = asyncio.Event()

    async def handle(self, event: AffectChanged) -> None:
        self.events.append(event)
        self._arrived.set()

    async def wait_for(self, count: int) -> None:
        """Block until at least *count* events have landed, or fail the test.

        The deadline is a module constant rather than a parameter: every call wants the same
        "this should be instant, so a wedge is a bug" semantics, and a per-call timeout is the
        knob that lets a genuinely slow path get papered over.
        """
        async with asyncio.timeout(_ARRIVAL_TIMEOUT_S):
            while len(self.events) < count:
                self._arrived.clear()
                if len(self.events) >= count:
                    return
                await self._arrived.wait()

    async def settle(self) -> None:
        """Let the bus drain whatever is queued, for the no-op assertions.

        Yielding round the loop is the honest way to say "nothing more is coming": a
        publish reaches its subscriber within a couple of scheduler turns, so if the
        collector is still empty after several, nothing was published.
        """
        for _ in range(10):
            await asyncio.sleep(0)


class Rig(NamedTuple):
    """Everything a test needs, wired the way the composition root will wire it."""

    service: AffectService
    bus: AsyncioEventBus
    collector: _Collector
    clock: FakeClock


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    """A started bus with the service's *declared* subscriptions registered by the caller —
    the same two steps ``main.py`` will take in #73."""
    clock = FakeClock()
    bus = AsyncioEventBus()
    service = AffectService(bus=bus, clock=clock)
    collector = _Collector()

    _register(bus, service)
    bus.subscribe(AffectChanged, collector.handle, name="test.collector")

    await bus.start()
    await service.start()
    try:
        yield Rig(service, bus, collector, clock)
    finally:
        await service.stop()
        await bus.stop()


def _register(bus: AsyncioEventBus, service: AffectService) -> None:
    """Register what the service *declared*. This is the composition root's job (SDS §9.2);
    the test does it here because #73 has not written it yet."""
    for sub in service.subscriptions():
        bus.subscribe(
            sub.event_type,
            sub.handler,
            name=sub.name,
            policy=sub.policy,
            maxsize=sub.maxsize,
        )


def _transitioned(
    *,
    to: RobotState,
    from_: RobotState = RobotState.IDLE,
    correlation_id: UUID | None = None,
    clock: FakeClock | None = None,
) -> StateTransitioned:
    """A ``state.transitioned`` as ``StateManager`` would publish it."""
    source = clock or FakeClock()
    return StateTransitioned(
        event_id=uuid4(),
        correlation_id=correlation_id or uuid4(),
        timestamp_ms=source.now() * 1000,
        monotonic_ns=source.monotonic_ns(),
        source="StateManager",
        from_=from_,
        to=to,
        trigger=Trigger.SYSTEM_STARTED,
    )


# --- AC-2: the Tier-1 map ------------------------------------------------------------------


def test_tier1_covers_the_robot_state_enum_exactly() -> None:
    """Exhaustive on purpose, the same bargain ``test_no_undocumented_transitions`` makes: a
    new ``RobotState`` without a face fails here, in milliseconds, rather than raising inside
    a bus handler on the Pi. There is no default entry precisely so this test is load-bearing.
    """
    assert set(_TIER1) == set(RobotState)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (RobotState.IDLE, Affect.IDLE),
        (RobotState.LISTENING, Affect.LISTENING),
        (RobotState.THINKING, Affect.THINKING),
        (RobotState.SPEAKING, Affect.SPEAKING),
        (RobotState.SLEEPING, Affect.SLEEPING),
        (RobotState.BOOTING, Affect.IDLE),
        (RobotState.DEGRADED, Affect.IDLE),
    ],
)
def test_each_state_maps_to_its_documented_face(
    state: RobotState, expected: Affect
) -> None:
    """Spelled out row by row so a reviewer can diff the table against SDS §6.8 directly."""
    assert _TIER1[state] is expected


def test_tier1_values_are_all_real_affects() -> None:
    assert all(isinstance(value, Affect) for value in _TIER1.values())


# --- AC-1: ports only, declarations only ---------------------------------------------------


def test_subscriptions_are_declared_not_registered() -> None:
    """The service says what it wants; the composition root decides (SDS §9.2).

    Constructing the service must leave the bus untouched — that inversion is what keeps the
    subscriber graph static and knowable for the §9.1.5 drift check.
    """
    bus = AsyncioEventBus()
    service = AffectService(bus=bus, clock=FakeClock())

    declared = service.subscriptions()
    assert [sub.event_type for sub in declared] == [StateTransitioned]
    assert all(sub.name for sub in declared)  # §9.1.5: never anonymous
    assert bus._subs == {}  # nothing registered itself behind the root's back


async def test_registration_must_happen_before_the_bus_starts() -> None:
    """Why declarations exist at all: the bus freezes its graph at ``start()``."""
    bus = AsyncioEventBus()
    service = AffectService(bus=bus, clock=FakeClock())
    await bus.start()
    try:
        with pytest.raises(RuntimeError):
            _register(bus, service)
    finally:
        await bus.stop()


# --- AC-3: the two tiers -------------------------------------------------------------------


async def test_a_state_transition_publishes_tier_1(
    rig: Rig,
) -> None:
    service, bus, collector, _ = rig
    await bus.publish(_transitioned(to=RobotState.LISTENING))
    await collector.wait_for(1)

    (event,) = collector.events
    assert event.affect is Affect.LISTENING
    assert event.tier == 1
    assert event.previous is Affect.IDLE
    assert event.source == "AffectService"
    assert service.affect is Affect.LISTENING


async def test_set_affect_publishes_tier_2(
    rig: Rig,
) -> None:
    service, bus, collector, _ = rig
    await service.set_affect(Affect.HAPPY, correlation_id=uuid4())
    await collector.wait_for(1)

    (event,) = collector.events
    assert event.affect is Affect.HAPPY
    assert event.tier == 2
    assert event.previous is Affect.IDLE
    assert service.affect is Affect.HAPPY


async def test_a_tier_2_overlay_survives_until_the_next_tier_1_change(
    rig: Rig,
) -> None:
    """SDS §6.8's core promise, in three beats.

    The overlay wins while it stands, a second overlay replaces it, and the next operational
    transition clears it back to the baseline. That last beat is what stops the robot grinning
    through "I have finished speaking".
    """
    service, bus, collector, _ = rig

    await service.set_affect(Affect.HAPPY, correlation_id=uuid4())
    await collector.wait_for(1)
    assert service.affect is Affect.HAPPY

    await service.set_affect(Affect.CONFUSED, correlation_id=uuid4())
    await collector.wait_for(2)
    assert service.affect is Affect.CONFUSED
    assert collector.events[1].tier == 2

    await bus.publish(_transitioned(to=RobotState.SPEAKING))
    await collector.wait_for(3)
    assert service.affect is Affect.SPEAKING
    assert collector.events[2].tier == 1
    assert collector.events[2].previous is Affect.CONFUSED


async def test_clearing_an_overlay_publishes_even_when_the_baseline_did_not_move(
    rig: Rig,
) -> None:
    """The case a baseline-only comparison gets wrong.

    SPEAKING + a HAPPY overlay, then playback finishes and the state returns to a baseline
    that maps to the *same* face the overlay was hiding. The blended answer still changed —
    HAPPY is no longer showing — so exactly one Tier-1 event must publish.
    """
    service, bus, collector, _ = rig

    await bus.publish(_transitioned(to=RobotState.SPEAKING))
    await collector.wait_for(1)
    await service.set_affect(Affect.HAPPY, correlation_id=uuid4())
    await collector.wait_for(2)

    await bus.publish(_transitioned(to=RobotState.SPEAKING))
    await collector.wait_for(3)

    assert collector.events[2].affect is Affect.SPEAKING
    assert collector.events[2].previous is Affect.HAPPY
    assert collector.events[2].tier == 1


async def test_previous_chains_correctly_across_several_transitions(
    rig: Rig,
) -> None:
    """``previous`` is what a subscriber animates *from*, so an off-by-one here would show up
    as the face crossfading from the wrong expression."""
    service, bus, collector, _ = rig

    for state in (RobotState.LISTENING, RobotState.THINKING, RobotState.SPEAKING):
        await bus.publish(_transitioned(to=state))
    await collector.wait_for(3)

    assert [e.affect for e in collector.events] == [
        Affect.LISTENING,
        Affect.THINKING,
        Affect.SPEAKING,
    ]
    assert [e.previous for e in collector.events] == [
        Affect.IDLE,
        Affect.LISTENING,
        Affect.THINKING,
    ]


# --- AC-4: no-op suppression ---------------------------------------------------------------


async def test_a_transition_to_the_same_face_publishes_nothing(
    rig: Rig,
) -> None:
    """BOOTING -> IDLE at boot: both map to IDLE, which is already current.

    Queues are DROP_OLDEST, so a redundant publish can *evict* a real one — and every
    redundant event downstream is a wasted render. Silence is the correct output.
    """
    service, bus, collector, _ = rig
    await bus.publish(_transitioned(from_=RobotState.BOOTING, to=RobotState.IDLE))
    await collector.settle()
    assert collector.events == []


async def test_setting_the_affect_that_is_already_showing_publishes_nothing(
    rig: Rig,
) -> None:
    """The Tier-2 flavour: the model asking for the face already on screen is a no-op."""
    service, bus, collector, _ = rig
    await service.set_affect(Affect.IDLE, correlation_id=uuid4())
    await collector.settle()
    assert collector.events == []
    assert service.affect is Affect.IDLE


async def test_two_states_that_share_a_face_publish_once(
    rig: Rig,
) -> None:
    """DEGRADED and IDLE both map to IDLE. Arriving at the second must be silent."""
    service, bus, collector, _ = rig

    await bus.publish(_transitioned(to=RobotState.LISTENING))
    await collector.wait_for(1)
    await bus.publish(_transitioned(to=RobotState.DEGRADED))
    await collector.wait_for(2)
    await bus.publish(_transitioned(to=RobotState.IDLE))
    await collector.settle()

    assert len(collector.events) == 2
    assert collector.events[1].affect is Affect.IDLE


# --- AC-5: the envelope --------------------------------------------------------------------


async def test_envelope_times_come_from_the_injected_clock(
    rig: Rig,
) -> None:
    """Wall clock for humans, monotonic for arithmetic (SDS §9.1.1). Advancing a ``FakeClock``
    is what proves these were *read* rather than sampled from the system clock — a real clock
    would pass a naive assertion by coincidence."""
    service, bus, collector, clock = rig
    await clock.advance(42.0)

    await bus.publish(_transitioned(to=RobotState.THINKING))
    await collector.wait_for(1)

    (event,) = collector.events
    assert event.timestamp_ms == clock.now() * 1000
    assert event.monotonic_ns == clock.monotonic_ns()


async def test_correlation_id_is_propagated_from_the_triggering_event(
    rig: Rig,
) -> None:
    """Minted only at a turn's origin, propagated everywhere after (SDS §9.1.1). If this
    service minted a fresh id, one grep would no longer reconstruct the turn — which is the
    entire reason the field exists."""
    service, bus, collector, _ = rig
    turn = uuid4()

    await bus.publish(_transitioned(to=RobotState.LISTENING, correlation_id=turn))
    await collector.wait_for(1)
    assert collector.events[0].correlation_id == turn


async def test_a_second_turns_id_does_not_leak_into_the_first(
    rig: Rig,
) -> None:
    """Two turns, two ids, no cross-contamination — the service holds no id of its own."""
    service, bus, collector, _ = rig
    first, second = uuid4(), uuid4()

    await bus.publish(_transitioned(to=RobotState.LISTENING, correlation_id=first))
    await collector.wait_for(1)
    await service.set_affect(Affect.HAPPY, correlation_id=second)
    await collector.wait_for(2)

    assert collector.events[0].correlation_id == first
    assert collector.events[1].correlation_id == second


async def test_event_ids_are_unique_per_publish(
    rig: Rig,
) -> None:
    service, bus, collector, _ = rig
    await bus.publish(_transitioned(to=RobotState.LISTENING))
    await collector.wait_for(1)
    await service.set_affect(Affect.SAD, correlation_id=uuid4())
    await collector.wait_for(2)

    assert collector.events[0].event_id != collector.events[1].event_id


# --- the §9.2 service shape ----------------------------------------------------------------


async def test_start_and_stop_are_trivial_and_stop_is_idempotent() -> None:
    """This service owns no tasks, so both are no-ops — but §9.2 requires them to exist and
    ``stop()`` to be safe to call twice (the lifecycle may unwind a partially-started rig)."""
    service = AffectService(bus=AsyncioEventBus(), clock=FakeClock())
    await service.start()
    await service.stop()
    await service.stop()
    assert service.name == "AffectService"
    assert service.affect is Affect.IDLE
