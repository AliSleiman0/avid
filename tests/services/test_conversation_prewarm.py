"""Presence-warmed sessions (ADR-014, #157, SDS §6.3.1) — open on presence, stream on speech.

A person walks in, the socket opens; they speak, the turn finds it warm. Nothing streams before
the VAD fires (unchanged from ADR-007), so every test here is about **when the socket is up**,
never about audio. ``hold_open=True`` on the replay keeps the fake's stream alive after its
(empty) timeline, as a real session stays up — and ``vendor_close()`` is the 60-minute lifetime
the vendor was measured to enforce (F-12).

Same rig as ``test_conversation.py``: real bus, real ``StateManager``, ``ReplayRealtimeClient``,
``FakeClock``, no mocks. Each guard's neuter is named on its test.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from avid.adapters.clock import FakeClock
from avid.adapters.realtime import ReplayRealtimeClient
from avid.core.envelope import envelope
from avid.domain import (
    ConversationSessionLost,
    RobotState,
    SystemHandlerFailed,
    Trigger,
    VisionPresenceGained,
    VisionPresenceLost,
)
from tests.services.test_conversation import (
    Rig,
    _advance_until,
    _fire_trigger,
    _rig,
    _speak,
    _yield,
)


async def _presence(
    rig: Rig, *, gained: bool, correlation_id: UUID | None = None
) -> None:
    """Publish the presence fact the hysteresis filter would publish (§9.1.3)."""
    env = envelope(
        clock=rig.clock,
        correlation_id=correlation_id or uuid4(),
        source="PresenceService",
    )
    if gained:
        await rig.bus.publish(VisionPresenceGained(**env, confidence=0.9))
    else:
        await rig.bus.publish(VisionPresenceLost(**env, absent_for_s=20.0))
    await _yield(rig)


def _warm_client(clock: FakeClock, **kwargs: object) -> ReplayRealtimeClient:
    return ReplayRealtimeClient(clock=clock, timeline=(), hold_open=True, **kwargs)  # type: ignore[arg-type]


async def test_presence_opens_the_socket_and_the_first_utterance_finds_it_warm() -> (
    None
):
    """The feature: open on presence, stream on speech.

    Neuter: drop the ``_warm`` branch in ``_on_speech_started`` and the utterance opens a SECOND
    socket — ``opens`` reads 2, hits 0."""
    clock = FakeClock()
    async with _rig(
        client=_warm_client(clock), initial=RobotState.IDLE, prewarm="presence"
    ) as rig:
        await _presence(rig, gained=True)
        assert rig.service._session_open is True
        assert rig.client.opens == 1
        assert rig.service.prewarm_opens() == 1
        assert rig.client.sent == [], "a warm socket must stream nothing"

        await _speak(rig, correlation_id=uuid4())
        await _yield(rig)
        assert rig.client.opens == 1, "the utterance opened a second socket"
        assert rig.service.prewarm_hits() == 1
        assert rig.service.prewarm_misses() == 0


async def test_presence_lost_closes_a_warm_socket_but_not_a_used_one() -> None:
    """Nobody spoke: close. Somebody spoke: the idle timer owns it, as it always did.

    Neuter: remove the teardown in ``_on_presence_lost`` and the first assertion reads True."""
    clock = FakeClock()
    async with _rig(
        client=_warm_client(clock),
        initial=RobotState.IDLE,
        prewarm="presence",
        session_idle_close_s=600,
    ) as rig:
        await _presence(rig, gained=True)
        assert rig.service._session_open is True
        await _presence(rig, gained=False)
        assert rig.service._session_open is False, "a warm socket outlived the person"

        await _presence(rig, gained=True)
        await _speak(rig, correlation_id=uuid4())
        await _yield(rig)
        await _presence(rig, gained=False)
        assert rig.service._session_open is True, (
            "presence lost closed a session a turn was using — the idle timer's call"
        )


async def test_entering_sleeping_closes_a_warm_socket() -> None:
    """A sleeping robot holds no socket (ADR-014). Driven through the real state machine so the
    synchronous observer arms the close the way ``main`` wires it."""
    clock = FakeClock()
    async with _rig(
        client=_warm_client(clock), initial=RobotState.IDLE, prewarm="presence"
    ) as rig:
        await _presence(rig, gained=True)
        assert rig.service._session_open is True

        await rig.state.transition(
            Trigger.PRESENCE_LOST_TIMEOUT, correlation_id=uuid4()
        )
        await _yield(rig)
        assert rig.state.state is RobotState.SLEEPING
        assert rig.service._session_open is False, "the robot slept holding a socket"


async def test_a_vendor_close_on_a_warm_socket_re_opens_while_present_and_is_bounded() -> (
    None
):
    """The measured 60-minute lifetime (F-12): re-open after the backoff, up to the cap, and
    never degrade — no turn was on the socket, so DEGRADED would be a lie.

    Neuter: drop the ``< self._prewarm_reopens_max`` check and ``opens`` reads 4, not 3."""
    clock = FakeClock()
    async with _rig(
        client=_warm_client(clock),
        initial=RobotState.IDLE,
        prewarm="presence",
        prewarm_reopens_max=2,
        prewarm_reopen_backoff_s=5.0,
    ) as rig:
        await _presence(rig, gained=True)
        assert rig.client.opens == 1

        for expected_opens in (2, 3):
            rig.client.vendor_close()
            await _yield(rig)
            assert rig.service._session_open is False
            await _advance_until(
                rig, lambda: rig.service._session_open, step_s=1.0, max_steps=10
            )
            assert rig.client.opens == expected_opens
        assert rig.service.prewarm_vendor_closes() == 2

        rig.client.vendor_close()
        await _yield(rig)
        # Past the cap: give a would-be re-open more than its backoff, and expect nothing.
        for _ in range(10):
            await rig.clock.advance(1.0)
            await _yield(rig)
        assert rig.client.opens == 3, "the third close was re-opened past the cap"
        assert rig.service.prewarm_vendor_closes() == 3
        assert rig.state.state is RobotState.IDLE, (
            "a warm vendor close degraded the robot"
        )
        assert rig.collector.of_type(ConversationSessionLost) == []


async def test_a_refused_warm_open_leaves_the_state_untouched_and_retries_on_the_next_edge() -> (
    None
):
    """The #452 shape, on the warm path: a refused connect must not wedge anything, and the
    next presence edge tries again — no loop (AVID-105)."""
    clock = FakeClock()
    client = _warm_client(clock, open_error="refused")
    async with _rig(client=client, initial=RobotState.IDLE, prewarm="presence") as rig:
        await _presence(rig, gained=True)
        assert rig.service._session_open is False
        assert rig.state.state is RobotState.IDLE
        assert rig.collector.of_type(SystemHandlerFailed) == []

        client.open_error = None
        await _presence(rig, gained=False)
        await _presence(rig, gained=True)
        assert rig.service._session_open is True
        assert rig.service.prewarm_opens() == 1


async def test_the_spend_ceiling_refuses_a_warm_open_too() -> None:
    """#472's guard covers the warm path: a socket that exists to be refused on is a socket that
    should not exist. Neuter: drop the ``_refused_on_spend`` call in ``_on_presence_gained``."""
    clock = FakeClock()
    async with _rig(
        client=_warm_client(clock),
        initial=RobotState.IDLE,
        prewarm="presence",
        hourly_ceiling_usd=0.0,
    ) as rig:
        await _presence(rig, gained=True)
        assert rig.service._session_open is False
        assert rig.service.spend_refusals() == 1
        assert rig.service.prewarm_opens() == 0


async def test_prewarm_never_leaves_presence_alone() -> None:
    """The schema default: ADR-007 unamended. Presence does nothing; speech opens cold, and a
    cold open with warming OFF is not a miss — nothing was promised."""
    clock = FakeClock()
    async with _rig(client=_warm_client(clock), initial=RobotState.IDLE) as rig:
        await _presence(rig, gained=True)
        assert rig.service._session_open is False
        assert rig.client.opens == 0

        await _speak(rig, correlation_id=uuid4())
        await _yield(rig)
        assert rig.client.opens == 1
        assert rig.service.prewarm_misses() == 0


async def test_a_cold_open_with_prewarm_on_is_counted_as_a_miss() -> None:
    """The measurement ADR-014 is graded on: a person who spoke before presence warmed the
    socket (or with nobody seen) paid the cold open, and the counter says so."""
    clock = FakeClock()
    async with _rig(
        client=_warm_client(clock), initial=RobotState.IDLE, prewarm="presence"
    ) as rig:
        await _speak(rig, correlation_id=uuid4())
        await _yield(rig)
        assert rig.client.opens == 1
        assert rig.service.prewarm_misses() == 1
        assert rig.service.prewarm_hits() == 0


async def test_a_proactive_turn_on_a_warm_socket_reopens_cold_with_its_context() -> (
    None
):
    """A reminder needs the §10.8 block in its instructions, and a warm open composed without
    it — so the warm socket is closed and the proactive path opens cold, with the block."""
    clock = FakeClock()
    async with _rig(
        client=_warm_client(clock), initial=RobotState.IDLE, prewarm="presence"
    ) as rig:
        await _presence(rig, gained=True)
        assert rig.client.opens == 1
        await _fire_trigger(rig, correlation_id=uuid4())
        assert rig.client.opens == 2
        assert rig.client.proactive_turns == 1
        assert rig.service.prewarm_misses() == 0, "a reminder is not a missed warm hit"
