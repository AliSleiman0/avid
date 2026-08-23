"""Tests for ``StateManager`` — the one owner of ``RobotState`` (AVID-69, SDS §3.8.4).

Three guarantees, in descending order of how much a bug would cost:

1. A legal transition moves the state **and** publishes exactly one ``state.transitioned``
   carrying the caller's ``correlation_id`` — the fact #71/#72 will build the face on.
2. An **illegal** one changes nothing and publishes nothing. The domain raises; the
   production policy (SDS §3.10.3) is logged-and-ignored, and it lives here.
3. Concurrent transitions serialize on the lock, so the published chain is coherent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID, uuid4

import pytest

from avid.adapters import FakeClock
from avid.core.event_bus import AsyncioEventBus
from avid.core.state_manager import StateManager
from avid.domain import Event, RobotState, StateTransitioned, Trigger


def _manager(
    bus: AsyncioEventBus, clock: FakeClock, initial: RobotState = RobotState.BOOTING
) -> StateManager:
    return StateManager(bus=bus, clock=clock, initial=initial)


def _recorder(
    bus: AsyncioEventBus, *, expect: int = 1
) -> tuple[list[Event], asyncio.Event]:
    """Record ``StateTransitioned`` deliveries; the flag fires once *expect* have landed.

    Delivery is asynchronous and :meth:`AsyncioEventBus.stop` *cancels* workers rather
    than draining them, so a test must await the flag — never a sleep, which would be
    both slow and a lie about what it is waiting for.
    """
    seen: list[Event] = []
    arrived = asyncio.Event()

    async def _handle(event: StateTransitioned) -> None:
        seen.append(event)
        if len(seen) >= expect:
            arrived.set()

    bus.subscribe(StateTransitioned, _handle, name="test-recorder")
    return seen, arrived


# --- the happy path ---------------------------------------------------------


async def test_transition_moves_state_and_returns_it() -> None:
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    mgr = _manager(bus, clock)
    assert mgr.state is RobotState.BOOTING

    async with bus:
        reached = await mgr.transition(Trigger.SYSTEM_STARTED, correlation_id=uuid4())

    assert reached is RobotState.IDLE
    assert mgr.state is RobotState.IDLE


async def test_transition_publishes_one_state_transitioned_with_the_callers_id() -> (
    None
):
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    mgr = _manager(bus, clock)
    seen, arrived = _recorder(bus)
    boot_id = uuid4()

    async with bus:
        await mgr.transition(Trigger.SYSTEM_STARTED, correlation_id=boot_id)
        await asyncio.wait_for(arrived.wait(), timeout=1.0)

    assert len(seen) == 1
    event = seen[0]
    assert isinstance(event, StateTransitioned)
    assert event.name == "state.transitioned"
    assert event.from_ is RobotState.BOOTING
    assert event.to is RobotState.IDLE
    assert event.trigger is Trigger.SYSTEM_STARTED
    # Propagated, never re-minted: one grep on it reconstructs the turn (SDS §3.12.2).
    assert event.correlation_id == boot_id
    assert event.source == "StateManager"
    # Envelope stamped from the injected clock (epoch seconds -> ms).
    assert event.timestamp_ms == clock.now() * 1000


async def test_state_is_already_current_when_the_event_lands() -> None:
    """The event is a *fact*, past tense (P4): a subscriber that reads back the manager
    must never see the pre-transition state."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    mgr = _manager(bus, clock)
    observed: list[RobotState] = []
    arrived = asyncio.Event()

    async def _handle(event: StateTransitioned) -> None:
        observed.append(mgr.state)
        arrived.set()

    bus.subscribe(StateTransitioned, _handle, name="reads-back")

    async with bus:
        await mgr.transition(Trigger.SYSTEM_STARTED, correlation_id=uuid4())
        await asyncio.wait_for(arrived.wait(), timeout=1.0)

    assert observed == [RobotState.IDLE]


# --- the illegal path (SDS §3.10.3) -----------------------------------------


async def test_illegal_transition_changes_nothing_and_publishes_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    mgr = _manager(bus, clock, initial=RobotState.SPEAKING)
    seen, _arrived = _recorder(bus)
    corr = uuid4()

    with caplog.at_level(logging.WARNING, logger="avid.state"):
        async with bus:
            # SPEAKING + system.started is not a row in the §3.10.3 table.
            result = await mgr.transition(Trigger.SYSTEM_STARTED, correlation_id=corr)

    assert result is RobotState.SPEAKING
    assert mgr.state is RobotState.SPEAKING
    assert seen == []
    # Logged and ignored, never fatal — and traceable to the turn that attempted it.
    assert "illegal transition" in caplog.text
    assert "SPEAKING" in caplog.text
    assert str(corr) in caplog.text


async def test_illegal_transition_does_not_raise() -> None:
    """A stray event in the wrong state must not take the robot down."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    mgr = _manager(bus, clock, initial=RobotState.IDLE)

    async with bus:
        await mgr.transition(Trigger.AUDIO_PLAYBACK_FINISHED, correlation_id=uuid4())

    assert mgr.state is RobotState.IDLE


# --- serialization ----------------------------------------------------------


async def test_concurrent_transitions_serialize_into_a_coherent_chain() -> None:
    """The lock (SDS §3.8.4) makes bus order match transition order: each event's
    ``from_`` is the previous event's ``to``, with no interleaving."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    mgr = _manager(bus, clock)
    seen, arrived = _recorder(bus, expect=3)

    async with bus:
        # BOOTING -> IDLE -> LISTENING -> THINKING, all fired at once.
        await asyncio.gather(
            mgr.transition(Trigger.SYSTEM_STARTED, correlation_id=uuid4()),
            mgr.transition(Trigger.AUDIO_SPEECH_STARTED, correlation_id=uuid4()),
            mgr.transition(Trigger.AUDIO_SPEECH_ENDED, correlation_id=uuid4()),
        )
        await asyncio.wait_for(arrived.wait(), timeout=1.0)

    assert mgr.state is RobotState.THINKING
    assert len(seen) == 3
    chain = [(e.from_, e.to) for e in seen if isinstance(e, StateTransitioned)]
    for (_, earlier_to), (later_from, _) in zip(chain, chain[1:]):
        assert earlier_to is later_from


async def test_initial_state_is_injectable() -> None:
    """Production always boots from BOOTING; tests may start mid-machine."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    assert _manager(bus, clock).state is RobotState.BOOTING
    assert (
        _manager(bus, clock, initial=RobotState.SLEEPING).state is RobotState.SLEEPING
    )


# --- the rejected-transition counter (#456) ---


async def test_the_counter_separates_a_wedge_from_ordinary_noise() -> None:
    """⚠️ Per pair, and that is the entire design (#456).

    A single total cannot grade this, because some rejections are **expected and documented**:
    `PresenceService` drives `VISION_PRESENCE_GAINED` unconditionally by design (#224), so an
    awake robot logs one every time somebody sits down. The M11 rig's own boot is the shape this
    has to distinguish — one pair at 135 and a benign pair at 1 — and a scalar would have added
    them together and told you nothing.
    """
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    mgr = _manager(bus, clock, initial=RobotState.THINKING)

    async with bus:
        # The wedge: the 10-minute nap timer, arriving over and over in a state with no row.
        for _ in range(135):
            await mgr.transition(Trigger.PRESENCE_LOST_TIMEOUT, correlation_id=uuid4())
        # ...and the documented benign one, which happens once when a person sits down.
        await mgr.transition(Trigger.VISION_PRESENCE_GAINED, correlation_id=uuid4())

    assert mgr.illegal_transitions() == {
        "THINKING/PRESENCE_LOST_TIMEOUT": 135,
        "THINKING/VISION_PRESENCE_GAINED": 1,
    }


async def test_a_legal_transition_counts_nothing() -> None:
    """The counter is about the moves that did NOT happen. A robot that works reports `{}`.

    Empty is a real reading — *nothing has been rejected* — not an absent one. `MetricsRegistry`
    only routes `None` to `absent`, so `{}` reaches `/metrics` as itself, which is the honest
    answer and the one a soak wants to see for thirty days.
    """
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    mgr = _manager(bus, clock, initial=RobotState.IDLE)

    async with bus:
        await mgr.transition(Trigger.AUDIO_SPEECH_STARTED, correlation_id=uuid4())

    assert mgr.state is RobotState.LISTENING
    assert mgr.illegal_transitions() == {}


async def test_the_reading_is_a_copy_and_survives_json() -> None:
    """Two properties a metrics provider owes, asserted together because both are about the reader.

    ⚠️ **A copy**: a caller that mutated the returned map would corrupt the tally of the only
    shared mutable object in the process. ⚠️ **JSON-native**: the keys are pre-rendered strings
    because a tuple or enum key raises `TypeError` inside `GET /metrics`' `json.dumps` and takes
    the **whole endpoint** down with a 500 — one unreadable metric costing every other one.
    """
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    mgr = _manager(bus, clock, initial=RobotState.SPEAKING)

    async with bus:
        await mgr.transition(Trigger.SYSTEM_STARTED, correlation_id=uuid4())

    reading = mgr.illegal_transitions()
    reading["SPEAKING/SYSTEM_STARTED"] = 999
    reading["invented"] = 1
    assert mgr.illegal_transitions() == {"SPEAKING/SYSTEM_STARTED": 1}
    assert json.loads(json.dumps(mgr.illegal_transitions())) == {
        "SPEAKING/SYSTEM_STARTED": 1
    }


# --- observers (#452) -------------------------------------------------------


async def test_an_observer_sees_every_legal_move_and_no_rejected_one() -> None:
    """``watch`` is the seam the §6.9 deadline hangs off, so it must see moves, not attempts.

    A rejected transition changes nothing and publishes nothing; telling an observer about it
    would let a service arm something on a state the machine is not in."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    mgr = _manager(bus, clock, initial=RobotState.IDLE)
    seen: list[tuple[RobotState, RobotState, Trigger, UUID]] = []

    def observer(
        *,
        from_: RobotState,
        to: RobotState,
        trigger: Trigger,
        correlation_id: UUID,
    ) -> None:
        seen.append((from_, to, trigger, correlation_id))

    mgr.watch(observer, name="test.observer")
    corr = uuid4()

    async with bus:
        await mgr.transition(Trigger.AUDIO_SPEECH_STARTED, correlation_id=corr)
        # Not a row: IDLE + system.started. Nothing moved, so nothing to observe.
        await mgr.transition(Trigger.SYSTEM_STARTED, correlation_id=uuid4())

    assert seen == [
        (RobotState.IDLE, RobotState.LISTENING, Trigger.AUDIO_SPEECH_STARTED, corr)
    ]


async def test_a_raising_observer_does_not_lose_the_move(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one thing this class exists never to lose is the transition itself.

    So a broken reaction is swallowed and logged, exactly as the bus does for a raising
    subscriber (SDS §3.5) — and by name, because an observer nobody can identify from the log is
    the reason ``watch`` makes *name* mandatory. The observer registered after the raising one
    still runs: one bad reaction must not silence the rest."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    mgr = _manager(bus, clock, initial=RobotState.IDLE)
    later: list[RobotState] = []

    def boom(**_: object) -> None:
        raise RuntimeError("observer bug")

    def after(*, to: RobotState, **_: object) -> None:
        later.append(to)

    mgr.watch(boom, name="test.broken")  # type: ignore[arg-type]  # deliberately wrong shape
    mgr.watch(after, name="test.after")  # type: ignore[arg-type]  # **kwargs stand-in

    with caplog.at_level(logging.ERROR, logger="avid.state"):
        async with bus:
            result = await mgr.transition(
                Trigger.AUDIO_SPEECH_STARTED, correlation_id=uuid4()
            )

    assert result is RobotState.LISTENING
    assert mgr.state is RobotState.LISTENING
    assert later == [RobotState.LISTENING]
    assert "test.broken" in caplog.text
