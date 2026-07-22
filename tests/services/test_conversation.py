"""The conversation loop: session lifecycle, conversation.* facts, degrade/recover (#102).

Real :class:`AsyncioEventBus`, the real ``ReplayRealtimeClient`` fed the committed
``assets/sessions/`` fixtures (#101), ``FakeTurnSink``, ``FakeClock``, a real
:class:`StateManager`, a real ``CueBank`` over the shipped ``assets/cues`` WAVs — **no mocks**
(``unittest.mock`` is banned outside ``tests/adapters/``, SDS §14.3). The properties under test
are about *dispatch, port calls and state edges*, which a mock would assert away rather than
exercise.

Time is virtual: the replay paces its events on the injected ``FakeClock``, so a test advances
the clock instead of waiting. ``_advance_until`` steps time forward until a predicate holds,
stopping early so a drain never runs long enough to trip the idle-close timer it is not testing.
Correlation is asserted by *identity* — every fact of a turn must carry the id minted on the
``audio.speech_started`` that opened it (SDS §3.12.2).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import NamedTuple
from uuid import UUID, uuid4

import pytest

from avid.adapters.clock import FakeClock
from avid.adapters.realtime import ReplayRealtimeClient
from avid.adapters.speaker import FakeSpeaker
from avid.adapters.turn_sink import FakeTurnSink
from avid.core.envelope import envelope
from avid.core.event_bus import AsyncioEventBus
from avid.core.hal import AudioChunk
from avid.core.state_manager import StateManager
from avid.domain import (
    AudioSpeechEnded,
    AudioSpeechStarted,
    ConversationAssistantResponded,
    ConversationSessionLost,
    ConversationTurnEnded,
    ConversationTurnStarted,
    ConversationUserTranscribed,
    Event,
    RobotState,
    StateTransitioned,
    SystemDegradedEntered,
    SystemDegradedExited,
    SystemHandlerFailed,
    TokenUsage,
    Trigger,
)
from avid.services import CueBank
from avid.services.conversation import ConversationService

_SESSIONS = Path(__file__).resolve().parents[2] / "assets" / "sessions"
_CUES = Path(__file__).resolve().parents[2] / "assets" / "cues"

# Every fact a test asserts on, plus the bus's own failure event and the state fact.
_COLLECTED: tuple[type[Event], ...] = (
    ConversationTurnStarted,
    ConversationUserTranscribed,
    ConversationAssistantResponded,
    ConversationTurnEnded,
    ConversationSessionLost,
    SystemDegradedEntered,
    SystemDegradedExited,
    StateTransitioned,
    SystemHandlerFailed,
)


class _Collector:
    """Records every collected event, with an awaitable arrival signal (mirrors test_audio)."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._arrived = asyncio.Event()

    async def handle(self, event: Event) -> None:
        self.events.append(event)
        self._arrived.set()

    def of_type(self, cls: type[Event]) -> list[Event]:
        return [e for e in self.events if isinstance(e, cls)]

    async def settle(self) -> None:
        """Let queued dispatch drain, for the 'nothing more happened' assertions."""
        for _ in range(10):
            await asyncio.sleep(0)


class Rig(NamedTuple):
    """Everything a test needs, wired the way ``main._wire_services`` wires the loop (#102)."""

    service: ConversationService
    bus: AsyncioEventBus
    clock: FakeClock
    state: StateManager
    client: ReplayRealtimeClient
    sink: FakeTurnSink
    speaker: FakeSpeaker
    collector: _Collector


_ExtraSub = tuple[type[Event], Callable[[Event], Awaitable[None]], str]


@contextlib.asynccontextmanager
async def _rig(
    *,
    client: ReplayRealtimeClient,
    initial: RobotState = RobotState.LISTENING,
    session_idle_close_s: int = 30,
    mic_script: tuple[AudioChunk, ...] = (),
    extra_subs: tuple[_ExtraSub, ...] = (),
) -> AsyncIterator[Rig]:
    """A started bus + running ConversationService driven by *client*'s recorded session.

    ``initial`` defaults to LISTENING because a user turn's first state edge is
    LISTENING→THINKING (AudioService already drove IDLE→LISTENING before ConvSvc sees the
    turn). All ``subscribe()`` calls precede ``bus.start()``; the service and bus are torn
    down on exit.
    """
    clock = client_clock(client)
    bus = AsyncioEventBus(clock=clock)
    state = StateManager(bus=bus, clock=clock, initial=initial)
    sink = FakeTurnSink(script=mic_script)
    speaker = FakeSpeaker()
    cues = CueBank(speaker=speaker, asset_dir=_CUES)
    collector = _Collector()
    service = ConversationService(
        bus=bus,
        clock=clock,
        state=state,
        client=client,
        sink=sink,
        cues=cues,
        session_idle_close_s=session_idle_close_s,
    )
    for sub in service.subscriptions():
        bus.subscribe(
            sub.event_type,
            sub.handler,
            name=sub.name,
            policy=sub.policy,
            maxsize=sub.maxsize,
        )
    for cls in _COLLECTED:
        bus.subscribe(cls, collector.handle, name=f"test.{cls.__name__}")
    for event_type, handler, name in extra_subs:
        bus.subscribe(event_type, handler, name=name)

    await bus.start()
    await service.start()
    try:
        yield Rig(service, bus, clock, state, client, sink, speaker, collector)
    finally:
        await service.stop()
        await bus.stop()


def client_clock(client: ReplayRealtimeClient) -> FakeClock:
    """The ``FakeClock`` a replay client was built on — so the rig drives the same time source."""
    clock = client._clock
    assert isinstance(clock, FakeClock)  # the tests always inject a FakeClock
    return clock


def _replay(name: str, *, clock: FakeClock) -> ReplayRealtimeClient:
    return ReplayRealtimeClient.from_dir(_SESSIONS / name, clock=clock)


async def _speak(rig: Rig, *, correlation_id: UUID) -> None:
    """Publish an ``audio.speech_started`` turn origin carrying *correlation_id*."""
    await rig.bus.publish(
        AudioSpeechStarted(
            **envelope(clock=rig.clock, correlation_id=correlation_id, source="test"),
            ring_buffer_ms=0,
        )
    )


async def _advance_until(
    rig: Rig, pred: Callable[[], bool], *, step_s: float = 0.5, max_steps: int = 400
) -> None:
    """Step virtual time forward until *pred* holds, yielding so dispatch can run.

    Stops the instant the predicate is satisfied, so a fixture drain never accumulates enough
    virtual time to trip an idle-close it is not exercising. Fails loudly if it never holds."""
    for _ in range(max_steps):
        if pred():
            return
        await rig.clock.advance(step_s)
        for _ in range(3):
            await asyncio.sleep(0)
    if not pred():
        raise AssertionError("predicate never became true within the step budget")


# --- the service shape (SDS §9.2) ----------------------------------------------------------


async def test_service_shape_declares_the_two_audio_origins() -> None:
    """AC-1/AC-2: name plus exactly the two ``audio.*`` origins that exist at M5 (the
    ``behavior.trigger_fired`` origin has no Event type yet — it is an M6 seam)."""
    clock = FakeClock()
    async with _rig(client=_replay("two_turn", clock=clock)) as rig:
        assert rig.service.name == "ConversationService"
        subs = rig.service.subscriptions()
        assert {s.event_type for s in subs} == {AudioSpeechStarted, AudioSpeechEnded}
        assert {s.name for s in subs} == {
            "ConversationService.speech_started",
            "ConversationService.speech_ended",
        }


# --- AC-4 / AC-7: a full turn --------------------------------------------------------------


async def test_two_turn_publishes_the_facts_on_one_correlation_id() -> None:
    """A full replayed session mints the four turn facts, all carrying the id minted on
    ``audio.speech_started`` (AC-4), assistant PCM reaches the sink tagged by item (AC-6), and
    ``turn_ended`` carries the real ``TokenUsage`` (AC-7)."""
    cid = uuid4()
    clock = FakeClock()
    async with _rig(client=_replay("two_turn", clock=clock)) as rig:
        await _speak(rig, correlation_id=cid)
        await _advance_until(
            rig, lambda: len(rig.collector.of_type(ConversationTurnEnded)) >= 2
        )

        # Per-type facts, not cross-type order (#72): two turns in the fixture ⇒ two of each.
        assert len(rig.collector.of_type(ConversationTurnStarted)) == 2
        assert len(rig.collector.of_type(ConversationUserTranscribed)) == 2
        assert len(rig.collector.of_type(ConversationAssistantResponded)) == 2
        ended = rig.collector.of_type(ConversationTurnEnded)
        assert len(ended) == 2

        # Every conversation fact propagates the one origin id — never re-minted.
        conversation_facts = [
            e
            for e in rig.collector.events
            if isinstance(
                e,
                ConversationTurnStarted
                | ConversationUserTranscribed
                | ConversationAssistantResponded
                | ConversationTurnEnded,
            )
        ]
        assert all(e.correlation_id == cid for e in conversation_facts)

        # AC-7: the real per-turn usage crossed intact.
        assert isinstance(ended[0], ConversationTurnEnded)
        assert ended[0].usage == TokenUsage(
            input_tokens=320, cached_input_tokens=256, output_tokens=48
        )

        # AC-6: assistant PCM was pushed to the sink, tagged by response item.
        assert [item_id for item_id, _ in rig.sink.played] == ["item_0", "item_1"]

        # #103: each completed turn finalizes playback through the sink (end_response), so the
        # real AudioService sink drives audio.playback_finished + SPEAKING→IDLE.
        assert rig.sink.responses_ended == 2


async def test_user_transcribed_drives_listening_to_thinking() -> None:
    """AC-5: ConvSvc drives the LISTENING→THINKING edge by direct call on the first
    ``user_transcribed`` — the one state edge that is genuinely this service's."""
    clock = FakeClock()
    async with _rig(
        client=_replay("two_turn", clock=clock), initial=RobotState.LISTENING
    ) as rig:
        await _speak(rig, correlation_id=uuid4())
        await _advance_until(rig, lambda: rig.state.state is RobotState.THINKING)

        moved = [
            e
            for e in rig.collector.of_type(StateTransitioned)
            if isinstance(e, StateTransitioned)
            and e.trigger is Trigger.CONVERSATION_USER_TRANSCRIBED
        ]
        assert moved and isinstance(moved[0], StateTransitioned)
        assert moved[0].from_ is RobotState.LISTENING
        assert moved[0].to is RobotState.THINKING


# --- AC-6: mic PCM forwarded up ------------------------------------------------------------


async def test_mic_frames_are_forwarded_up_to_the_client() -> None:
    """AC-6: captured mic PCM off the ``TurnSink`` is sent up to the model. Uses an empty
    replay timeline so only the mic-forward path runs."""
    clock = FakeClock()
    frames = (
        AudioChunk(pcm=b"\x01\x02", sample_rate=16000, channels=1),
        AudioChunk(pcm=b"\x03\x04", sample_rate=16000, channels=1),
    )
    client = ReplayRealtimeClient(clock=clock, timeline=())
    async with _rig(client=client, mic_script=frames) as rig:
        await _speak(rig, correlation_id=uuid4())
        await rig.collector.settle()
        assert tuple(rig.client.sent) == frames


# --- AC-3: idle close ----------------------------------------------------------------------


async def test_session_closes_after_idle_timeout() -> None:
    """AC-3: with no further speech, the session is closed after ``session_idle_close_s``.
    An empty timeline keeps the session open-but-quiet so only the idle path fires."""
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())
    async with _rig(client=client, session_idle_close_s=5) as rig:
        await _speak(rig, correlation_id=uuid4())
        await rig.collector.settle()
        assert rig.client.opened and not rig.client.closed
        await _advance_until(rig, lambda: rig.client.closed, step_s=2.0)
        assert rig.client.closed


# --- AC-4 / AC-5 / AC-6: degrade and recover (UC-06) ---------------------------------------


async def test_session_loss_degrades_then_reopen_recovers() -> None:
    """AC-4/5/6: a mid-turn drop publishes ``session_lost`` + ``system.degraded_entered``,
    moves any→DEGRADED, and plays the canned CueBank phrase; the next speech re-opens a cold
    session and drives DEGRADED→IDLE + ``system.degraded_exited`` (reopen-on-next-speech, the
    recovery ``replay`` affords)."""
    clock = FakeClock()
    async with _rig(
        client=_replay("session_loss", clock=clock), initial=RobotState.LISTENING
    ) as rig:
        await _speak(rig, correlation_id=uuid4())
        await _advance_until(rig, lambda: rig.state.state is RobotState.DEGRADED)

        lost = rig.collector.of_type(ConversationSessionLost)
        assert lost and isinstance(lost[0], ConversationSessionLost)
        assert lost[0].cause == "network" and lost[0].was_mid_turn is True
        assert len(rig.collector.of_type(SystemDegradedEntered)) == 1
        # The degraded cue was played through the shared speaker (best-effort, §6.9).
        await _advance_until(
            rig,
            lambda: any(
                p.name == "lost_connection.wav" for p in rig.speaker.files_played
            ),
        )

        # Recovery: a fresh turn re-opens and exits degraded before it could re-drop.
        await _speak(rig, correlation_id=uuid4())
        await _advance_until(
            rig, lambda: len(rig.collector.of_type(SystemDegradedExited)) == 1
        )
        exited = rig.collector.of_type(SystemDegradedExited)
        assert isinstance(exited[0], SystemDegradedExited)
        assert exited[0].downtime_s >= 0.0
        assert any(
            isinstance(e, StateTransitioned)
            and e.trigger is Trigger.SYSTEM_DEGRADED_EXITED
            and e.to is RobotState.IDLE
            for e in rig.collector.of_type(StateTransitioned)
        )


# --- AC-8: reliability — a raising subscriber never kills the turn -------------------------


async def test_a_raising_subscriber_does_not_kill_the_turn() -> None:
    """AC-8 / the headline reliability property (§3.5.2): a subscriber that raises on
    ``conversation.user_transcribed`` is swallowed and republished as ``system.handler_failed``
    — the turn still reaches ``turn_ended`` and the bus stays up."""

    async def boom(_event: Event) -> None:
        raise RuntimeError("subscriber blew up")

    clock = FakeClock()
    async with _rig(
        client=_replay("two_turn", clock=clock),
        extra_subs=((ConversationUserTranscribed, boom, "test.boom"),),
    ) as rig:
        await _speak(rig, correlation_id=uuid4())
        await _advance_until(
            rig, lambda: len(rig.collector.of_type(ConversationTurnEnded)) >= 1
        )
        # The turn survived the raising peer, and the bus reported the failure as a fact.
        assert rig.collector.of_type(ConversationTurnEnded)
        failed = rig.collector.of_type(SystemHandlerFailed)
        assert any(
            isinstance(e, SystemHandlerFailed) and e.handler == "test.boom"
            for e in failed
        )


async def test_speech_ended_with_a_live_session_rearms_the_idle_timer() -> None:
    """AC-2: while a session is open, ``audio.speech_ended`` re-arms the idle-close countdown
    rather than closing anything — a pause in speech is not the end of the session."""
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())
    async with _rig(client=client, session_idle_close_s=5) as rig:
        await _speak(rig, correlation_id=uuid4())
        await rig.collector.settle()
        await rig.bus.publish(
            AudioSpeechEnded(
                **envelope(clock=rig.clock, correlation_id=uuid4(), source="test"),
                duration_ms=200,
            )
        )
        await rig.collector.settle()
        # The session stayed open across the pause; the idle timer is simply re-armed.
        assert rig.client.opened and not rig.client.closed


async def test_stop_is_idempotent_and_closes_the_session() -> None:
    """``stop`` tears a live session down and is safe to call twice (§9.2)."""
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())
    async with _rig(client=client) as rig:
        await _speak(rig, correlation_id=uuid4())
        await rig.collector.settle()
        assert rig.client.opened
    # The context manager already called stop(); the client is closed and re-stopping is clean.
    assert rig.client.closed
    await rig.service.stop()


@pytest.mark.parametrize("event_pair", [pytest.param(True, id="ended-before-started")])
async def test_speech_ended_without_a_session_is_a_noop(event_pair: bool) -> None:
    """#72 order-tolerance: an ``audio.speech_ended`` arriving with no open session (the two
    origins are separate subscriber queues) does nothing and does not raise."""
    assert event_pair
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())
    async with _rig(client=client) as rig:
        await rig.bus.publish(
            AudioSpeechEnded(
                **envelope(clock=rig.clock, correlation_id=uuid4(), source="test"),
                duration_ms=100,
            )
        )
        await rig.collector.settle()
        assert not rig.client.opened  # nothing opened a session
