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
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import NamedTuple
from uuid import UUID, uuid4

import pytest

from avid.adapters import (
    FakeEmbedder,
    FakeFactRepository,
    FakeTextModel,
    HybridRetriever,
)
from avid.adapters.clock import FakeClock
from avid.adapters.microphone import FakeMicrophone
from avid.adapters.realtime import ReplayRealtimeClient
from avid.adapters.speaker import FakeSpeaker
from avid.adapters.turn_sink import FakeTurnSink
from avid.adapters.vad import FakeVoiceActivityDetector
from avid.core.envelope import envelope
from avid.core.event_bus import AsyncioEventBus
from avid.core.hal import AudioChunk
from avid.core.realtime import (
    AssistantAudioChunk,
    ToolCallRequested,
    TurnDone,
    UserTranscript,
)
from avid.core.state_manager import StateManager
from avid.domain import (
    Affect,
    AudioPlaybackFinished,
    AudioSpeechEnded,
    AudioSpeechStarted,
    BehaviorTriggerFired,
    ConversationAssistantResponded,
    ConversationSessionLost,
    ConversationTurnEnded,
    ConversationTurnStarted,
    ConversationUserTranscribed,
    Direction,
    Event,
    Fact,
    LookAtResult,
    MemoryFactStored,
    RobotState,
    RoutineSpec,
    ScoreWeights,
    StateTransitioned,
    SystemDegradedEntered,
    SystemDegradedExited,
    SystemHandlerFailed,
    TokenUsage,
    Trigger,
)
from avid.services import CueBank, MemoryService
from avid.services.audio import AudioService
from avid.services.conversation import (
    _MEMORY_HEADER,
    ConversationService,
    compose_proactive_block,
)

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
    AudioPlaybackFinished,
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


class _StubMemory:
    """A :class:`~avid.core.ports.MemoryTools` double that records its calls — a fake, not a mock
    (SDS §14.3). Lets a conversation test assert which tool the dispatcher reached without standing
    up the whole memory stack; the AC-7 e2e uses a real ``MemoryService`` instead."""

    def __init__(
        self,
        *,
        recall_result: tuple[Fact, ...] = (),
        top_facts_result: tuple[Fact, ...] = (),
        top_facts_delay_s: float = 0.0,
        top_facts_error: Exception | None = None,
    ) -> None:
        self.remembered: list[tuple[str, str, int]] = []
        self.recalled: list[str] = []
        self.forgotten: list[str] = []
        self.top_facts_calls = 0
        self._recall_result = recall_result
        self._top_facts_result = top_facts_result
        self._top_facts_delay_s = top_facts_delay_s
        self._top_facts_error = top_facts_error

    async def remember_fact(
        self,
        text: str,
        kind: str,
        importance: int,
        *,
        schedule: RoutineSpec | None = None,
        correlation_id: UUID | None = None,
    ) -> int:
        self.remembered.append((text, kind, importance))
        return 1

    async def recall(
        self, query: str, *, k: int = 5, correlation_id: UUID | None = None
    ) -> tuple[Fact, ...]:
        self.recalled.append(query)
        return self._recall_result

    async def forget(self, query: str, *, correlation_id: UUID | None = None) -> int:
        self.forgotten.append(query)
        return 0

    async def top_facts(self) -> tuple[Fact, ...]:
        self.top_facts_calls += 1
        if self._top_facts_delay_s:
            await asyncio.sleep(self._top_facts_delay_s)
        if self._top_facts_error is not None:
            raise self._top_facts_error
        return self._top_facts_result


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
    memory: _StubMemory | MemoryService


_ExtraSub = tuple[type[Event], Callable[[Event], Awaitable[None]], str]


@contextlib.asynccontextmanager
async def _rig(
    *,
    client: ReplayRealtimeClient,
    initial: RobotState = RobotState.THINKING,
    session_idle_close_s: int = 30,
    mic_script: tuple[AudioChunk, ...] = (),
    extra_subs: tuple[_ExtraSub, ...] = (),
    memory: _StubMemory | MemoryService | None = None,
    memory_inject_timeout_s: float = 1.0,
    think_timeout_s: float = 3600.0,
    server_turn_detection: bool = False,
    thinking_delay_ms: int = 0,
    hold_open_s: float = 30.0,
) -> AsyncIterator[Rig]:
    """A started bus + running ConversationService driven by *client*'s recorded session.

    ``initial`` defaults to THINKING: by the time ConvSvc's pump sees a turn, AudioService has
    already driven IDLE→LISTENING on its rising edge **and** LISTENING→THINKING on its falling
    one (AVID-158), so THINKING is where a model-side turn actually begins. All ``subscribe()``
    calls precede ``bus.start()``; the service and bus are torn down on exit.

    ``think_timeout_s`` defaults to an hour of virtual time for the same reason
    ``session_idle_close_s`` is a knob here: :func:`_advance_until` drains up to 200 virtual
    seconds, so a realistic 10 s deadline would degrade any test that merely advances the clock
    while a turn is open — a failure that would look like a regression in whatever that test was
    actually about. Tests that exercise the deadline pass an explicit small value (AVID-171).
    """
    clock = client_clock(client)
    bus = AsyncioEventBus(clock=clock)
    state = StateManager(bus=bus, clock=clock, initial=initial)
    sink = FakeTurnSink(script=mic_script)
    speaker = FakeSpeaker()
    cues = CueBank(speaker=speaker, asset_dir=_CUES)
    collector = _Collector()
    mem = memory if memory is not None else _StubMemory()
    service = ConversationService(
        bus=bus,
        clock=clock,
        state=state,
        client=client,
        sink=sink,
        cues=cues,
        memory=mem,
        affect=_StubAffect(),
        behavior=_StubBehavior(),
        gesture=_StubGesture(),
        session_idle_close_s=session_idle_close_s,
        memory_inject_timeout_s=memory_inject_timeout_s,
        default_timezone="Asia/Beirut",
        hold_open_s=hold_open_s,
        think_timeout_s=think_timeout_s,
        server_turn_detection=server_turn_detection,
        thinking_delay_ms=thinking_delay_ms,
    )
    # The §6.9 deadline follows the state (#452) — `main._wire_services` registers this observer
    # right after building the service, and a rig that skipped it would be testing a robot nobody
    # ships. Direct call, not a subscription: see `StateManager.watch`.
    state.watch(service.on_transition, name="ConversationService.think_deadline")
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
        yield Rig(service, bus, clock, state, client, sink, speaker, collector, mem)
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


async def _utterance(rig: Rig, *, correlation_id: UUID) -> None:
    """Drive a whole utterance the way ``AudioService`` does — edges **and** transitions (#452).

    ``_speak`` publishes a fact and moves nothing, which is right for what it was written for and
    is exactly why no test in this file could see #452: the rig starts *inside* THINKING, so
    "the machine entered THINKING" was not an observable event here at all. This mirrors
    ``AudioService._begin_speech``/``_end_speech``: transition then publish on the rising edge,
    publish then transition on the falling one, in that order, because that is the order the real
    service uses and the order the bug lives in.

    From the rig's default THINKING this composes THINKING → LISTENING → THINKING; both rows
    exist, so it is legal from either start state the tests use.
    """
    await rig.state.transition(
        Trigger.AUDIO_SPEECH_STARTED, correlation_id=correlation_id
    )
    await rig.bus.publish(
        AudioSpeechStarted(
            **envelope(clock=rig.clock, correlation_id=correlation_id, source="test"),
            ring_buffer_ms=0,
        )
    )
    await rig.collector.settle()
    await rig.bus.publish(
        AudioSpeechEnded(
            **envelope(clock=rig.clock, correlation_id=correlation_id, source="test"),
            duration_ms=200,
        )
    )
    await rig.state.transition(
        Trigger.AUDIO_SPEECH_ENDED, correlation_id=correlation_id
    )
    await rig.collector.settle()


async def _finish_playback(
    bus: AsyncioEventBus,
    clock: FakeClock,
    *,
    item_id: str,
    played_ms: int,
    truncated: bool,
    correlation_id: UUID,
) -> None:
    """Publish the ``audio.playback_finished`` fact AudioService emits — the barge-in feed (#104).

    A ``truncated=True`` fact carrying ``played_ms`` is exactly what
    ``AudioService.interrupt`` publishes once local VAD has cut the speaker; the unit tests
    stand in for that so the ConvSvc model-side half (truncate/cancel/mute) can be exercised
    with a chosen ``played_ms`` and no AudioService (§6.2.4)."""
    await bus.publish(
        AudioPlaybackFinished(
            **envelope(clock=clock, correlation_id=correlation_id, source="test"),
            item_id=item_id,
            played_ms=played_ms,
            truncated=truncated,
        )
    )


async def _advance_clock_until(
    clock: FakeClock,
    pred: Callable[[], bool],
    *,
    step_s: float = 0.5,
    max_steps: int = 400,
) -> None:
    """Step *clock* forward until *pred* holds, yielding so dispatch can run.

    Stops the instant the predicate is satisfied, so a fixture drain never accumulates enough
    virtual time to trip an idle-close it is not exercising. Fails loudly if it never holds."""
    for _ in range(max_steps):
        if pred():
            return
        await clock.advance(step_s)
        for _ in range(3):
            await asyncio.sleep(0)
    if not pred():
        raise AssertionError("predicate never became true within the step budget")


async def _advance_until(
    rig: Rig, pred: Callable[[], bool], *, step_s: float = 0.5, max_steps: int = 400
) -> None:
    """Step the rig's virtual time until *pred* holds (see :func:`_advance_clock_until`)."""
    await _advance_clock_until(rig.clock, pred, step_s=step_s, max_steps=max_steps)


# --- the service shape (SDS §9.2) ----------------------------------------------------------


async def test_service_shape_declares_the_audio_origins_and_barge_in_feed() -> None:
    """AC-1/AC-2: name plus the two ``audio.*`` origins, the ``audio.playback_finished`` barge-in
    feed (#104), and — since #239 — ``behavior.trigger_fired``.

    That last one was a *declared seam* from M5 until M10: the second of §9.1.1's two turn origins,
    documented in this service's own ``subscriptions()`` docstring and deliberately unwired,
    because the event type did not exist. It exists now, and the seam is a subscription."""
    clock = FakeClock()
    async with _rig(client=_replay("two_turn", clock=clock)) as rig:
        assert rig.service.name == "ConversationService"
        subs = rig.service.subscriptions()
        assert {s.event_type for s in subs} == {
            AudioSpeechStarted,
            AudioSpeechEnded,
            AudioPlaybackFinished,
            BehaviorTriggerFired,
        }
        assert {s.name for s in subs} == {
            "ConversationService.speech_started",
            "ConversationService.speech_ended",
            "ConversationService.playback_finished",
            "ConversationService.trigger_fired",
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


async def test_tool_call_dispatches_recall_and_returns_the_output() -> None:
    """#125: a ``recall`` ``ToolCallRequested`` in the stream is dispatched against the memory port.

    The ``tool_call`` fixture interleaves a ``recall`` invocation (query "travel plans next month")
    between the user transcript and the assistant reply. The pump now dispatches it: the memory
    port's ``recall`` is called with that query, and the result is returned to the client via
    ``send_tool_output`` (echoed ``call_id``). The turn's facts still mint on the one correlation id
    and the assistant PCM still reaches the sink; nothing raises (no ``system.handler_failed``) —
    a memory write publishes ``memory.fact_stored`` from MemoryService, never a ``conversation.*``."""
    cid = uuid4()
    clock = FakeClock()
    memory = _StubMemory(
        recall_result=(
            Fact(
                id=1,
                text="a trip to Lisbon",
                kind="event",
                importance=6,
                created_at=0,
                last_accessed_at=0,
            ),
        )
    )
    async with _rig(client=_replay("tool_call", clock=clock), memory=memory) as rig:
        await _speak(rig, correlation_id=cid)
        await _advance_until(
            rig, lambda: len(rig.collector.of_type(ConversationTurnEnded)) >= 1
        )
        await rig.collector.settle()

        # The tool was dispatched to the memory port with the model's query.
        assert memory.recalled == ["travel plans next month"]
        # …and its result was returned to the client, echoing the fixture's call_id.
        assert rig.client.tool_outputs == [("call_0", rig.client.tool_outputs[0][1])]
        assert "Lisbon" in rig.client.tool_outputs[0][1]

        # The single turn's facts still mint normally, all on the origin id.
        assert len(rig.collector.of_type(ConversationTurnStarted)) == 1
        assert len(rig.collector.of_type(ConversationUserTranscribed)) == 1
        assert len(rig.collector.of_type(ConversationAssistantResponded)) == 1
        assert len(rig.collector.of_type(ConversationTurnEnded)) == 1
        assert [item_id for item_id, _ in rig.sink.played] == ["item_0"]
        assert rig.collector.of_type(SystemHandlerFailed) == []


async def test_remember_fact_on_an_approximate_turn_is_declined() -> None:
    """#125 barge-in guard (§6.2.4/§7.6): a ``remember_fact`` on a turn whose user transcript is
    approximate is **not** written — a half-heard tail stored as fact is the confabulation §7.6
    guards against. The model is told, honestly, that nothing was stored (a tool output goes back),
    and the memory port is never touched."""
    cid = uuid4()
    clock = FakeClock()
    timeline: tuple[tuple[int, object], ...] = (
        (0, UserTranscript(text="i think my name is... ", is_approximate=True)),
        (
            10,
            ToolCallRequested(
                call_id="call_x",
                name="remember_fact",
                arguments='{"text": "the user is called sam", "kind": "identity", "importance": 8}',
            ),
        ),
        (
            10,
            TurnDone(
                usage=TokenUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1)
            ),
        ),
    )
    client = ReplayRealtimeClient(clock=clock, timeline=timeline)  # type: ignore[arg-type]
    memory = _StubMemory()
    async with _rig(client=client, memory=memory) as rig:
        await _speak(rig, correlation_id=cid)
        await _advance_until(
            rig, lambda: len(rig.collector.of_type(ConversationTurnEnded)) >= 1
        )
        await rig.collector.settle()

        assert memory.remembered == []  # nothing written on an approximate turn
        assert len(rig.client.tool_outputs) == 1  # but the model still gets a reply
        assert "approximate" in rig.client.tool_outputs[0][1]
        assert rig.collector.of_type(SystemHandlerFailed) == []


async def test_remember_fact_lands_a_row_and_publishes_on_one_correlation_id() -> None:
    """AC-7: a full turn where the model calls ``remember_fact`` against a **real** ``MemoryService``
    (over the P6 fakes) — the row lands durably, ``memory.fact_stored`` publishes, the tool output is
    returned, and every fact of the turn is on the one ``correlation_id``, zero network.

    Wired standalone rather than through ``_rig`` because ``MemoryService`` and
    ``ConversationService`` must share **one** bus (the memory write publishes ``memory.fact_stored``
    on it), which the rig builds privately."""
    cid = uuid4()
    clock = FakeClock()
    timeline: tuple[tuple[int, object], ...] = (
        (0, UserTranscript(text="my name is Ali", is_approximate=False)),
        (
            10,
            ToolCallRequested(
                call_id="call_r",
                name="remember_fact",
                arguments='{"text": "the user is called Ali", "kind": "identity", "importance": 9}',
            ),
        ),
        (
            10,
            TurnDone(
                usage=TokenUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1)
            ),
        ),
    )
    client = ReplayRealtimeClient(clock=clock, timeline=timeline)  # type: ignore[arg-type]
    bus = AsyncioEventBus(clock=clock)
    state = StateManager(bus=bus, clock=clock, initial=RobotState.LISTENING)
    repo = FakeFactRepository(clock=clock)
    embedder = FakeEmbedder()
    retriever = HybridRetriever(
        repo=repo,
        embedder=embedder,
        bus=bus,
        clock=clock,
        top_k=5,
        half_life_days=14.0,
        weights=ScoreWeights(),
    )
    memory = MemoryService(
        bus=bus,
        clock=clock,
        repo=repo,
        retriever=retriever,
        embedder=embedder,
        text_model=FakeTextModel(),
        supersession_threshold=0.85,
        supersession_k=5,
        forget_relevance_floor=0.60,
        forget_k=5,
        top_facts_max=15,
        top_facts_token_budget=600,
    )
    sink = FakeTurnSink(script=())
    service = ConversationService(
        bus=bus,
        clock=clock,
        state=state,
        client=client,
        sink=sink,
        cues=CueBank(speaker=FakeSpeaker(), asset_dir=_CUES),
        memory=memory,
        session_idle_close_s=30,
        memory_inject_timeout_s=1.0,
        default_timezone="Asia/Beirut",
        hold_open_s=30.0,
        think_timeout_s=3600.0,
        server_turn_detection=False,
        thinking_delay_ms=0,
        affect=_StubAffect(),
        behavior=_StubBehavior(),
        gesture=_StubGesture(),
    )
    ended: list[Event] = []
    stored: list[MemoryFactStored] = []

    async def _record_ended(event: Event) -> None:
        ended.append(event)

    async def _record_stored(event: Event) -> None:
        assert isinstance(event, MemoryFactStored)
        stored.append(event)

    for sub in service.subscriptions():
        bus.subscribe(
            sub.event_type,
            sub.handler,
            name=sub.name,
            policy=sub.policy,
            maxsize=sub.maxsize,
        )
    bus.subscribe(ConversationTurnEnded, _record_ended, name="test.turn_ended")
    bus.subscribe(MemoryFactStored, _record_stored, name="test.fact_stored")

    await bus.start()
    await memory.start()  # boot rebuild (empty store)
    await service.start()
    try:
        await bus.publish(
            AudioSpeechStarted(
                **envelope(clock=clock, correlation_id=cid, source="test"),
                ring_buffer_ms=0,
            )
        )
        # Advance virtual time (paces the replay so the tool call is emitted) AND yield real time —
        # the injection's top_facts() and the remember_fact write ride the store's real writer thread,
        # which a sleep(0)-only wait starves under coverage tracing.
        for _ in range(600):
            if ended and stored:
                break
            await clock.advance(0.05)
            await asyncio.sleep(0.005)
        assert ended and stored

        # The row is durable (a direct call inside the tool handler, §3.7.3).
        live = await repo.fetch_live()
        assert [f.text for f in live] == ["the user is called Ali"]
        # memory.fact_stored published, on the turn's correlation id (§3.12.2).
        assert len(stored) == 1
        assert stored[0].correlation_id == cid
        assert stored[0].kind == "identity"
        # The tool output was returned to the model, echoing the call_id.
        assert client.tool_outputs[0][0] == "call_r"
        assert '"ok": true' in client.tool_outputs[0][1]
    finally:
        await service.stop()
        await memory.stop()
        await bus.stop()


# --- #126: pre-session memory injection (§6.7 path 1) --------------------------------------


def test_format_memory_block_renders_a_bounded_bulleted_block() -> None:
    """The pure layer-4 formatter (§6.4): a short header plus one bullet per fact, in order; an
    empty set renders nothing so an empty memory injects the stateless prefix unchanged (AC-4)."""
    from avid.services.conversation import _format_memory_block

    assert _format_memory_block(()) == ""
    facts = (
        Fact(
            id=1,
            text="the user's name is Ali",
            kind="identity",
            importance=9,
            created_at=0,
            last_accessed_at=0,
        ),
        Fact(
            id=2,
            text="the user runs every morning",
            kind="routine",
            importance=6,
            created_at=0,
            last_accessed_at=0,
        ),
    )
    block = _format_memory_block(facts)
    assert block.splitlines() == [
        "What you already know about the user (from earlier conversations):",
        "- the user's name is Ali",
        "- the user runs every morning",
    ]


async def test_top_facts_are_injected_as_the_layer_4_block_at_open() -> None:
    """AC-2/AC-3: at session open the top facts are composed into the layer-4 block and handed to
    the client (here the replay records it on ``injected``). The fetch runs once per open."""
    clock = FakeClock()
    memory = _StubMemory(
        top_facts_result=(
            Fact(
                id=1,
                text="the user's name is Ali",
                kind="identity",
                importance=9,
                created_at=0,
                last_accessed_at=0,
            ),
        )
    )
    client = ReplayRealtimeClient(clock=clock, timeline=())
    async with _rig(client=client, memory=memory) as rig:
        await _speak(rig, correlation_id=uuid4())
        await _advance_until(rig, lambda: bool(rig.client.injected))
        assert memory.top_facts_calls == 1
        assert rig.client.injected == [
            "What you already know about the user (from earlier conversations):\n"
            "- the user's name is Ali"
        ]


async def test_empty_memory_injects_the_stateless_instruction(  # AC-4
) -> None:
    clock = FakeClock()
    memory = _StubMemory()  # no facts
    client = ReplayRealtimeClient(clock=clock, timeline=())
    async with _rig(client=client, memory=memory) as rig:
        await _speak(rig, correlation_id=uuid4())
        await _advance_until(rig, lambda: bool(rig.client.injected))
        assert memory.top_facts_calls == 1
        assert rig.client.injected == [""]  # empty block → the M5 prefix, unchanged


async def test_a_reconnect_re_seeds_the_memory(  # AC-5
) -> None:
    """Every open re-runs the fetch — a mid-conversation drop comes back as a cold session with
    the same facts re-injected (§6.2.3). Here: first speech opens, the session drops, the next
    speech re-opens, and ``top_facts`` has run twice."""
    clock = FakeClock()
    memory = _StubMemory()
    async with _rig(client=_replay("session_loss", clock=clock), memory=memory) as rig:
        await _speak(rig, correlation_id=uuid4())
        await _advance_until(rig, lambda: rig.state.state is RobotState.DEGRADED)
        await _speak(rig, correlation_id=uuid4())  # reopen-on-next-speech
        await _advance_until(
            rig, lambda: len(rig.collector.of_type(SystemDegradedExited)) == 1
        )
        assert memory.top_facts_calls == 2  # once per cold open


async def test_a_memory_failure_opens_the_session_anyway(  # AC-6
) -> None:
    """A retrieval failure at open is caught, logged with the correlation id, and degrades to an
    empty block — the robot still talks, it just does not remember this session."""
    clock = FakeClock()
    memory = _StubMemory(top_facts_error=RuntimeError("store is down"))
    client = ReplayRealtimeClient(clock=clock, timeline=())
    async with _rig(client=client, memory=memory) as rig:
        await _speak(rig, correlation_id=uuid4())
        await _advance_until(rig, lambda: rig.client.opened)
        assert rig.client.injected == [
            ""
        ]  # degraded to no memory, session still opened
        assert rig.collector.of_type(SystemHandlerFailed) == []  # not a bus failure


async def test_a_slow_memory_fetch_times_out_and_opens_anyway(  # AC-6
) -> None:
    """A hung store must not delay time-to-session-ready past budget: the fetch is bounded by
    ``memory_inject_timeout_s`` and a timeout degrades to an empty block. Real time here (not the
    FakeClock) because ``asyncio.wait_for``'s deadline is real-loop time."""
    clock = FakeClock()
    memory = _StubMemory(top_facts_delay_s=0.2)  # slower than the timeout below
    client = ReplayRealtimeClient(clock=clock, timeline=())
    async with _rig(client=client, memory=memory, memory_inject_timeout_s=0.01) as rig:
        await _speak(rig, correlation_id=uuid4())
        for _ in range(50):  # let the real wait_for deadline fire
            if rig.client.injected:
                break
            await asyncio.sleep(0.01)
        assert rig.client.injected == [""]


async def test_the_user_transcript_publishes_a_fact_but_drives_no_transition() -> None:
    """AVID-158: ``conversation.user_transcribed`` stays a published fact (§9.1.3) and stops
    being a state trigger.

    It is the model's separate transcription pass — measured on hardware arriving *after* the
    assistant's speech-to-speech audio, and once after ``conversation.turn_ended`` — so it
    cannot be the LISTENING→THINKING edge. AudioService drives that from its own
    ``audio.speech_ended`` falling edge, which is local and always fires.

    (This test's ``async def`` header was lost in an earlier edit: its body executed as the tail
    of the memory-timeout test above, under that test's name, asserting the pre-AVID-158
    behaviour it now contradicts.)"""
    clock = FakeClock()
    async with _rig(
        client=_replay("two_turn", clock=clock), initial=RobotState.THINKING
    ) as rig:
        await _speak(rig, correlation_id=uuid4())
        await _advance_until(
            rig, lambda: bool(rig.collector.of_type(ConversationUserTranscribed))
        )

        assert rig.collector.of_type(
            ConversationUserTranscribed
        )  # still a published fact
        assert rig.collector.of_type(StateTransitioned) == []  # and it moved nothing
        assert rig.state.state is RobotState.THINKING


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
    session and drives DEGRADED→LISTENING + ``system.degraded_exited`` (reopen-on-next-speech,
    the recovery ``replay`` affords).

    LISTENING, not IDLE, since AVID-162: the reopen is triggered *by* the user's rising edge, so
    recovery always lands mid-turn and rejoins it. This rig proves the service *announces* the
    recovery — the whole arc needs the real ``AudioService`` driving the audio edges, and is
    asserted in ``tests/e2e/test_m5_gate.py``."""
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
            and e.to is RobotState.LISTENING
            for e in rig.collector.of_type(StateTransitioned)
        )


# --- #104: barge-in truncate (SDS §6.2.4) --------------------------------------------------


async def test_barge_in_truncates_cancels_and_mutes() -> None:
    """AC-1/AC-2/AC-5: an ``audio.playback_finished(truncated=True)`` fact drives the §6.2.4
    model half — ``truncate(item_id, played_ms)`` then ``cancel`` — and the post-truncation
    delta of that item (``turn0_c``) is muted, never reaching the sink. ``played_ms`` is passed
    straight through as ``audio_end_ms``."""
    cid = uuid4()
    clock = FakeClock()
    async with _rig(client=_replay("barge_in", clock=clock)) as rig:
        await _speak(rig, correlation_id=cid)
        # Two item_0 deltas (turn0_a/b) reach the speaker, then the user barges in.
        await _advance_until(
            rig,
            lambda: len([i for i, _ in rig.sink.played if i == "item_0"]) >= 2,
        )
        await _finish_playback(
            rig.bus,
            rig.clock,
            item_id="item_0",
            played_ms=40,
            truncated=True,
            correlation_id=cid,
        )
        await (
            rig.collector.settle()
        )  # let _on_playback_finished arm the mute + truncate/cancel
        await _advance_until(
            rig, lambda: len(rig.collector.of_type(ConversationTurnEnded)) >= 1
        )

        # Steps 4/5: the model was told the user cut item_0 off at what actually played.
        assert rig.client.truncations == [("item_0", 40)]
        assert rig.client.cancels == 1
        # Step 6: only the two pre-barge deltas reached the sink — turn0_c was dropped (AC-2).
        assert [i for i, _ in rig.sink.played] == ["item_0", "item_0"]


async def test_barge_in_user_transcript_is_approximate() -> None:
    """AC-4: a barge-in leaves the truncated turn's transcript unreliable, so
    ``conversation.user_transcribed`` carries ``is_approximate=True`` (the flag the fixture sets
    and ConvSvc propagates, §6.2.4)."""
    clock = FakeClock()
    async with _rig(client=_replay("barge_in", clock=clock)) as rig:
        await _speak(rig, correlation_id=uuid4())
        await _advance_until(
            rig,
            lambda: len(rig.collector.of_type(ConversationUserTranscribed)) >= 2,
        )
        # The opening utterance is exact; the interrupting one is approximate (truncation tail).
        flags = [
            e.is_approximate
            for e in rig.collector.of_type(ConversationUserTranscribed)
            if isinstance(e, ConversationUserTranscribed)
        ]
        assert flags[:2] == [False, True]


async def test_normal_playback_finished_does_not_truncate() -> None:
    """A *normal* end (``truncated=False``) is AudioService's own fact — ConvSvc ignores it and
    sends no ``truncate``/``cancel`` (only a barge-in cuts the model off)."""
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())
    async with _rig(client=client) as rig:
        await _speak(rig, correlation_id=uuid4())
        await rig.collector.settle()
        await _finish_playback(
            rig.bus,
            rig.clock,
            item_id="item_0",
            played_ms=999,
            truncated=False,
            correlation_id=uuid4(),
        )
        await rig.collector.settle()
        assert rig.client.truncations == []
        assert rig.client.cancels == 0


async def test_barge_in_full_chain_on_one_correlation_id() -> None:
    """AC-5 (e2e): with the **real** ``AudioService`` as the sink, a local barge-in runs the
    whole §6.2.4 chain on one ``correlation_id`` — speaker stopped, the truncated
    ``audio.playback_finished`` published with what actually played, the model told
    (``truncate``+``cancel``), and the post-truncation delta muted (never reaching the speaker).

    The mic loop is never started; the turn's playback is driven through ConvSvc's pump and the
    local barge-in is triggered by ``AudioService.interrupt()`` directly — exactly what the VAD
    edge (``_begin_speech``) does, whose own SPEAKING→LISTENING move is covered in test_audio."""
    cid = uuid4()
    clock = FakeClock()
    client = _replay("barge_in", clock=clock)
    bus = AsyncioEventBus(clock=clock)
    # THINKING, not LISTENING: the mic loop is never started here, so nothing fires the falling
    # edge that would carry the machine there — and THINKING is where AudioService leaves it by
    # the time a reply's first delta arrives (AVID-158).
    state = StateManager(bus=bus, clock=clock, initial=RobotState.THINKING)
    speaker = FakeSpeaker()
    mic = FakeMicrophone(sample_rate=16000, channels=1, chunk_ms=20)
    vad = FakeVoiceActivityDetector(default=False)
    audio = AudioService(
        bus=bus,
        clock=clock,
        state=state,
        microphone=mic,
        speaker=speaker,
        vad=vad,
        ring_buffer_ms=300,
        sample_rate=16000,
        channels=1,
        silence_hold_ms=200,
        barge_in_margin_db=6.0,
        highpass_hz=150.0,
        highpass_order=3,
        echo_tail_ms=150,
        capture_stall_s=5.0,
        loopback=False,
    )
    cues = CueBank(speaker=speaker, asset_dir=_CUES)
    collector = _Collector()
    service = ConversationService(
        bus=bus,
        clock=clock,
        state=state,
        client=client,
        sink=audio,
        cues=cues,
        memory=_StubMemory(),
        session_idle_close_s=30,
        memory_inject_timeout_s=1.0,
        default_timezone="Asia/Beirut",
        hold_open_s=30.0,
        think_timeout_s=3600.0,
        server_turn_detection=False,
        thinking_delay_ms=0,
        affect=_StubAffect(),
        behavior=_StubBehavior(),
        gesture=_StubGesture(),
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
    await bus.start()
    await service.start()  # ConvSvc only — AudioService's mic loop stays parked
    try:
        # The id the mic-loop origin would have minted for this turn's playback (test_audio idiom).
        audio._turn_id = cid
        await bus.publish(
            AudioSpeechStarted(
                **envelope(clock=clock, correlation_id=cid, source="test"),
                ring_buffer_ms=0,
            )
        )
        # Drive the turn until the robot is genuinely SPEAKING with two deltas on the speaker.
        await _advance_clock_until(
            clock,
            lambda: state.state is RobotState.SPEAKING and len(speaker.played) >= 2,
        )

        # Local barge-in: cut the speaker and measure what actually played (§6.2.4 steps 2/3).
        played = await audio.interrupt()
        assert played > 0 and speaker.stops == 1
        await (
            collector.settle()
        )  # let ConvSvc arm the mute + truncate/cancel before turn0_c
        await _advance_clock_until(
            clock, lambda: len(collector.of_type(ConversationTurnEnded)) >= 1
        )

        # Steps 4/5: the model was told, with the honest audio_end_ms.
        assert client.truncations == [("item_0", played)]
        assert client.cancels == 1
        # Step 6: turn0_c never reached the real speaker — still just the two pre-barge deltas.
        assert len(speaker.played) == 2
        # One correlation_id: the truncated fact carries the same id as the turn facts.
        truncated = [
            e
            for e in collector.of_type(AudioPlaybackFinished)
            if isinstance(e, AudioPlaybackFinished) and e.truncated
        ]
        assert truncated and truncated[0].correlation_id == cid
        assert truncated[0].played_ms == played
    finally:
        await service.stop()
        await audio.stop()
        await bus.stop()


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


async def test_the_falling_edge_ends_the_user_turn() -> None:
    """AVID-194: with the server VAD off, our falling edge is the **only** thing that ends a turn.

    Previously two detectors decided this — ours at ``silence_hold_ms``, OpenAI's at
    ``silence_duration_ms`` — and since a 500–900 ms pause is ordinary speech, the server routinely
    committed inside one and answered a fragment. The failure this asserts against is the mirror
    image: with the server no longer committing, a falling edge that does not commit is a turn the
    model never hears at all, and nothing downstream will rescue it."""
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())
    async with _rig(client=client) as rig:
        await _speak(rig, correlation_id=uuid4())
        await rig.collector.settle()
        assert rig.client.committed_turns == 0  # still talking

        await rig.bus.publish(
            AudioSpeechEnded(
                **envelope(clock=rig.clock, correlation_id=uuid4(), source="test"),
                duration_ms=200,
            )
        )
        await rig.collector.settle()

    assert client.committed_turns == 1


async def test_the_falling_edge_does_not_commit_when_the_server_still_owns_turns() -> (
    None
):
    """The other half of the switch, and the reason it is a switch rather than a deletion.

    With ``[ai.turn_detection] type = "server_vad"`` the far end commits on its own clock, so a
    commit from here would be a *second* one — the two-authority race again, arriving from the
    opposite direction. The configuration is the defect and is not shipped, but it stays reachable
    for comparison, and reachable means correct."""
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())
    async with _rig(client=client, server_turn_detection=True) as rig:
        await _speak(rig, correlation_id=uuid4())
        await rig.bus.publish(
            AudioSpeechEnded(
                **envelope(clock=rig.clock, correlation_id=uuid4(), source="test"),
                duration_ms=200,
            )
        )
        await rig.collector.settle()

    assert client.committed_turns == 0


async def test_no_thinking_cue_when_the_reply_is_already_playing() -> None:
    """AVID-176: with a VAD margin the reply routinely beats our falling edge.

    The server commits on its own clock — ``[ai.turn_detection] silence_duration_ms`` after the
    user's last speech frame — **while we are still streaming**, so with the shipped 400 ms margin
    the first delta usually arrives ~400 ms before ``audio.speech_ended``. Arming the §6.9 cue at
    that falling edge would play *"one sec"* straight over a reply already coming out of the
    speaker: ``CueBank`` plays to the ``Speaker`` directly rather than through the ``TurnSink``
    (the AVID-158 defect), and two concurrent plays also break ``AlsaSpeaker``'s documented
    one-play-in-flight invariant.

    The §6.9 deadline is skipped on the same edge and for the same reason — the first token it
    exists to wait for has already arrived."""
    clock = FakeClock()
    async with _rig(
        client=_replay("two_turn", clock=clock), think_timeout_s=1.0
    ) as rig:
        await _speak(rig, correlation_id=uuid4())
        await rig.collector.settle()
        # The reply lands before the user's falling edge — the common case with a margin.
        await _advance_until(rig, lambda: bool(rig.sink.played))
        assert rig.service._first_audio is True

        cues_before = list(rig.speaker.files_played)
        await rig.bus.publish(
            AudioSpeechEnded(
                **envelope(clock=rig.clock, correlation_id=uuid4(), source="test"),
                duration_ms=200,
            )
        )
        await rig.collector.settle()

        assert rig.speaker.files_played == cues_before, (
            "the thinking cue was armed over a reply that was already playing"
        )
        assert rig.service._think_task is None, (
            "the §6.9 deadline was armed for a first token that had already arrived"
        )


class _StubBehavior:
    """A :class:`~avid.core.ports.BehaviorTools` double: records the quiet requests (#243)."""

    def __init__(self) -> None:
        self.quiets: list[int] = []

    async def set_quiet(self, duration_s: int, *, correlation_id: UUID) -> int:
        self.quiets.append(duration_s)
        return 1_800_003_600


class _StubAffect:
    """An ``AffectTools`` double for the conversation rig (AVID-214).

    Records rather than renders: the service's job is to route a tool call to the port, and what
    happens to the face afterwards is ``AffectService``'s and ``ExpressionService``'s business —
    neither of which this service is allowed to know exists (P5)."""

    def __init__(self) -> None:
        self.applied: list[Affect] = []

    async def set_affect(self, affect: Affect, *, correlation_id: UUID) -> None:
        self.applied.append(affect)


# The message a refused connect actually carries, shaped like the one the #106 bench logged. The
# type matters more than the text: the handler catches `OSError` at that call and nothing wider,
# because a broad `except Exception` would re-hide the genuine subscriber bugs the bus's
# swallow-and-republish exists to surface.
_REFUSED = (
    "Multiple exceptions: [Errno 111] Connect call failed ('162.159.140.245', 443)"
)


def _unreachable(clock: FakeClock) -> ReplayRealtimeClient:
    """A replay client whose ``open`` fails the way a dead network does (AVID-188, #452).

    This was a ``_UnreachableClient`` subclass living in this file until #452, and that was the
    tell: a failure mode only a test-local subclass can express is invisible to ``tests/e2e``,
    which is the only place the "no illegal transition" assertion bites — so the one test that
    would have caught a 24-hour wedge could not be written. The knob ships on the fake now (P6)."""
    return ReplayRealtimeClient(clock=clock, timeline=(), open_error=_REFUSED)


async def test_a_refused_open_does_not_park_the_robot_in_thinking() -> None:
    """#452 AC-1/AC-2: THINKING is never occupied without an armed way out.

    The 24-hour wedge, off hardware. ``AudioService`` drives ``LISTENING -> THINKING`` on its own
    falling edge whether or not anything downstream can act on it; the refused ``open()`` returns
    from ``_on_speech_started`` without touching the machine, and ``_on_speech_ended`` used to
    return at its ``_session_open`` guard before arming the only exit that needs neither a live
    session nor the user to speak again. On the rig that was 8,600x the designed bound, and every
    graded soak criterion passed throughout.

    The advance is a **fixed** 30 s rather than ``_advance_until`` so that neutering the fix fails
    this on its assertion, not on a helper's step budget — a red for the wrong reason is not a
    proof."""
    clock = FakeClock()
    client = _unreachable(clock)

    async with _rig(
        client=client, initial=RobotState.IDLE, think_timeout_s=10.0
    ) as rig:
        await _utterance(rig, correlation_id=uuid4())
        assert rig.state.state is RobotState.THINKING  # the wedge, as it was found
        assert rig.service._session_open is False  # ...with no session to get it out

        await rig.clock.advance(30.0)
        await rig.collector.settle()

        assert rig.state.state is RobotState.DEGRADED, (
            "the robot is parked in THINKING with no armed exit — #452's wedge"
        )
        moves = [
            (e.from_, e.trigger, e.to)
            for e in rig.collector.of_type(StateTransitioned)
            if isinstance(e, StateTransitioned)
        ]
        assert (
            RobotState.THINKING,
            Trigger.THINK_TIMEOUT,
            RobotState.DEGRADED,
        ) in moves
        # It gave up on a session that never opened; nothing dropped (§6.9, #106 AC-6).
        assert rig.collector.of_type(ConversationSessionLost) == []


async def test_the_wedge_a_refused_open_used_to_cause_is_now_countable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#456 AC-4, driven through the arc #452 actually failed on rather than a synthetic one.

    The rig's wedge was not a `transition()` call in a unit test — it was `PresenceService`'s nap
    timer arriving, every ten minutes for a day, at a machine parked in THINKING by a refused
    `open()`. This drives the same shape end to end: the refused open puts the machine in
    THINKING, and the triggers that used to bounce off it are now **countable by name**, so
    something outside the process can see which pair is repeating.

    ⚠️ The deadline is deliberately long here. #455 means the robot leaves THINKING on its own
    after `think_timeout_s`, which is the fix working — but this test is about what is *visible*
    while it is stuck, so the clock never reaches it."""
    clock = FakeClock()
    client = _unreachable(clock)

    with caplog.at_level(logging.WARNING, logger="avid.state"):
        async with _rig(
            client=client, initial=RobotState.IDLE, think_timeout_s=3600.0
        ) as rig:
            await _utterance(rig, correlation_id=uuid4())
            assert rig.state.state is RobotState.THINKING  # the wedge, as it was found

            # The nap timer, arriving where it has no row — 135 times on the rig.
            for _ in range(5):
                await rig.state.transition(
                    Trigger.PRESENCE_LOST_TIMEOUT, correlation_id=uuid4()
                )
            # ...and the benign one, which happens on any awake robot when someone sits down.
            await rig.state.transition(
                Trigger.VISION_PRESENCE_GAINED, correlation_id=uuid4()
            )

            assert rig.state.illegal_transitions() == {
                "THINKING/PRESENCE_LOST_TIMEOUT": 5,
                "THINKING/VISION_PRESENCE_GAINED": 1,
            }, "the wedge is invisible to everything outside the process"

    # The log line is still there and still says the same thing — counting is not a downgrade.
    assert "ignored illegal transition" in caplog.text


async def test_a_refused_proactive_open_does_not_park_the_robot_in_thinking() -> None:
    """The same defect on the proactive arc, where it is strictly worse (#452).

    ``BehaviorService`` drives ``IDLE -> THINKING`` **before** publishing
    ``behavior.trigger_fired``, so a refused open here wedges immediately and **no falling edge is
    ever coming** — there is no user in the room and nothing else will move the machine. A fix at
    the reactive call site would not have touched this, which is the argument for arming from the
    state rather than from the turn path."""
    clock = FakeClock()
    client = _unreachable(clock)

    async with _rig(
        client=client, initial=RobotState.IDLE, think_timeout_s=10.0
    ) as rig:
        turn = uuid4()
        await rig.state.transition(Trigger.BEHAVIOR_TRIGGER_FIRED, correlation_id=turn)
        await rig.bus.publish(
            BehaviorTriggerFired(
                **envelope(clock=rig.clock, correlation_id=turn, source="test"),
                trigger_id=1,
                fact_id=1,
            )
        )
        await rig.collector.settle()
        assert rig.state.state is RobotState.THINKING
        assert rig.service._session_open is False

        await rig.clock.advance(30.0)
        await rig.collector.settle()

        assert rig.state.state is RobotState.DEGRADED, (
            "a reminder the network refused parked the robot in THINKING forever"
        )


async def test_the_deadline_survives_a_falling_edge_that_beats_its_rising_one() -> None:
    """The third arc, and the one with no network fault in it at all (#452, #72).

    The bus is FIFO per subscriber, **not across**, so ``audio.speech_ended`` can reach this
    service before ``audio.speech_started`` has opened the session — on a perfectly healthy
    network. ``_on_speech_ended`` returns early in that case, so arming from there armed nothing;
    ``_arm_the_deadline`` carried a ``settle()`` specifically to dodge it, and a workaround in a
    helper is not a covered case. Published here with **no settle between the edges**, which is
    what the real bus can deliver."""
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())  # healthy, answers nothing

    async with _rig(
        client=client, initial=RobotState.IDLE, think_timeout_s=10.0
    ) as rig:
        turn = uuid4()
        await rig.state.transition(Trigger.AUDIO_SPEECH_STARTED, correlation_id=turn)
        await rig.bus.publish(
            AudioSpeechEnded(
                **envelope(clock=rig.clock, correlation_id=turn, source="test"),
                duration_ms=200,
            )
        )
        await rig.bus.publish(
            AudioSpeechStarted(
                **envelope(clock=rig.clock, correlation_id=turn, source="test"),
                ring_buffer_ms=0,
            )
        )
        await rig.state.transition(Trigger.AUDIO_SPEECH_ENDED, correlation_id=turn)
        await rig.collector.settle()

        assert rig.state.state is RobotState.THINKING
        assert rig.service._think_task is not None, (
            "the deadline was lost to cross-subscriber ordering, with nothing wrong at all"
        )


async def test_a_failed_reconnect_stays_degraded_instead_of_escaping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AVID-188: speaking while the network is down is expected, not a handler crash.

    On the #106 AC-6 recovery run the owner spoke four times while degraded, and each
    ``client.open()`` raised ``OSError`` **out of** the handler — four full tracebacks. The bus
    did exactly its job (logged, swallowed, republished ``system.handler_failed``), which is why
    the run survived and is also why this is worth fixing at the source:

    * the DoD requires new failure paths to *"log with a correlation ID"*, and a raw traceback
      through the bus meets that only by luck — the id appears in the bus's own preamble;
    * ``system.handler_failed`` is the event the bus reserves for genuine subscriber bugs, so
      routine network failure inflates the one signal that exists to catch them;
    * four tracebacks per outage is enough noise to hide a real defect, and that run had two
      other findings underneath them.

    The assertion is therefore about **where** the failure is handled, not merely that the robot
    survived: no ``system.handler_failed``, one WARNING carrying the turn's correlation id, and
    the session still closed so the next rising edge retries."""
    clock = FakeClock()
    client = _unreachable(clock)

    async with _rig(client=client) as rig:
        turn = uuid4()
        with caplog.at_level(logging.WARNING, logger="avid.services.conversation"):
            await _speak(rig, correlation_id=turn)
            await rig.collector.settle()

        assert client.injected == [""]  # one open attempt, memory resolved not leaked
        assert rig.service._session_open is False  # nothing half-opened
        assert rig.collector.of_type(SystemHandlerFailed) == [], (
            "a routine network failure reached system.handler_failed — the event the bus "
            "reserves for genuine subscriber bugs"
        )
        assert str(turn) in caplog.text
        assert "staying degraded" in caplog.text

        # The next utterance retries rather than giving up on the session for good.
        await _speak(rig, correlation_id=uuid4())
        await rig.collector.settle()
        assert len(client.injected) == 2


async def test_a_fast_turn_plays_no_thinking_cue_at_all() -> None:
    """AVID-170 AC-1/AC-5, the near edge: first audio at 300 ms against a 600 ms threshold.

    §6.9 specifies a *threshold*, not a delay — *"if first audio hasn't arrived by 600 ms"*. There
    was no timer at all: the cue was scheduled immediately and only cancellation stopped it, a
    race the cue reliably won because it starts pushing a WAV to ALSA in the same tick the user
    stops speaking. So it played on **every** turn. Reported by the owner as *"hearing a lot of
    one second"* and first suspected to be a network problem; it is a defect against spec.

    The inverse of R-01, too: perceived latency is designable, and an unconditional filler makes
    a fast turn *sound* slow."""
    clock = FakeClock()
    # The threshold is set far beyond the fixture's own first-delta delay rather than the
    # shipped 600 ms, so "the reply arrived first" is true by construction instead of by
    # whatever the recording happens to be paced at. The property under test is the ordering,
    # not the number.
    async with _rig(
        client=_replay("two_turn", clock=clock), thinking_delay_ms=5_000
    ) as (rig):
        await _speak(rig, correlation_id=uuid4())
        await rig.bus.publish(
            AudioSpeechEnded(
                **envelope(clock=rig.clock, correlation_id=uuid4(), source="test"),
                duration_ms=200,
            )
        )
        await _advance_until(rig, lambda: bool(rig.sink.played))

        assert rig.service._thinking_task is None  # the delta cancelled the pending cue
        assert not rig.speaker.files_played

        # ...and it stays uncued long after the threshold would have expired.
        await rig.clock.advance(10.0)
        await rig.collector.settle()

        assert not rig.speaker.files_played, (
            "a turn faster than the threshold still played a cue — AVID-170"
        )


async def test_a_slow_turn_plays_exactly_one_thinking_cue() -> None:
    """The far edge (AC-5): nothing has arrived by the threshold, so the silence gets covered.

    A robot that visibly and audibly thinks feels responsive at 1200 ms; one that sits silently
    feels broken at 800 ms (§6.9). This is the case the cue exists for, and the only one."""
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())
    async with _rig(client=client, thinking_delay_ms=600) as rig:
        await _speak(rig, correlation_id=uuid4())
        await rig.bus.publish(
            AudioSpeechEnded(
                **envelope(clock=rig.clock, correlation_id=uuid4(), source="test"),
                duration_ms=200,
            )
        )
        await rig.collector.settle()
        assert not rig.speaker.files_played  # not yet — still inside the threshold

        await _advance_until(
            rig,
            lambda: any(p.name == "thinking_hmm.wav" for p in rig.speaker.files_played),
        )

    assert sum(p.name == "thinking_hmm.wav" for p in rig.speaker.files_played) == 1


async def test_the_thinking_cue_is_armed_at_the_falling_edge_not_at_the_transcript() -> (
    None
):
    """§6.9 / AVID-158: the filler covers ``speech_ended`` → first audio, so it is armed on the
    falling edge.

    Armed on the transcript instead, the bench measured it firing 1.1 s *after* the assistant
    had started speaking (t=52.482 playback vs t=53.594 transcript) and once after
    ``conversation.turn_ended`` — and ``CueBank`` plays straight to the ``Speaker``, not through
    the ``TurnSink``, so it was talking over the reply it exists to cover."""
    clock = FakeClock()
    client = ReplayRealtimeClient(
        clock=clock, timeline=()
    )  # no transcript ever arrives
    async with _rig(client=client) as rig:
        await _speak(rig, correlation_id=uuid4())
        await rig.collector.settle()
        assert not rig.speaker.files_played  # nothing yet — the user is still talking

        await rig.bus.publish(
            AudioSpeechEnded(
                **envelope(clock=rig.clock, correlation_id=uuid4(), source="test"),
                duration_ms=200,
            )
        )
        await _advance_until(
            rig,
            lambda: any(p.name == "thinking_hmm.wav" for p in rig.speaker.files_played),
        )


async def test_the_first_assistant_delta_cancels_a_pending_thinking_cue() -> None:
    """The cue is best-effort filler, so the reply cuts it off: the first delta of the turn
    clears the latch and cancels the task, whatever else is in flight (§6.9)."""
    clock = FakeClock()
    async with _rig(client=_replay("two_turn", clock=clock)) as rig:
        await _speak(rig, correlation_id=uuid4())
        await rig.bus.publish(
            AudioSpeechEnded(
                **envelope(clock=rig.clock, correlation_id=uuid4(), source="test"),
                duration_ms=200,
            )
        )
        await _advance_until(rig, lambda: bool(rig.sink.played))

        assert rig.service._first_audio is True
        assert rig.service._thinking_task is None


# --- the §6.9 first-token deadline (AVID-171) ----------------------------------------------


async def test_a_reply_that_beats_one_falling_edge_does_not_disarm_every_later_turn() -> (
    None
):
    """The ``_first_audio`` latch is re-armed at **every** falling edge, not only the ones that
    reach the thinking cue (AVID-186).

    The regression this pins cost the #106 seal run. The reset lived in ``_start_thinking_cue``,
    which the "a reply is already playing" branch returns *before* reaching — so the first time a
    reply beat a falling edge the latch stuck ``True`` for the rest of the session. Since AVID-176
    gave the local hold a deliberate 400 ms margin over the server VAD, a reply beating the
    falling edge is the NORMAL case, usually on turn one. Every later turn then read
    ``already_replying`` as ``True`` and armed neither the cue nor the §6.9 deadline: on the bench
    the robot waited **41 s** through a network outage in silence with the deadline set to 10 s.

    So the assertion is deliberately about the *second* turn. Asserting on the first proves
    nothing — the first turn armed its cue correctly even with the bug.

    ⚠️ **The subject moved with #452 and the assertion had to move with it.** The latch no longer
    decides the §6.9 deadline — that is armed by the entry into THINKING now, so asserting on
    ``_think_task`` here would pass with the latch bug fully restored, which is a green test
    proving nothing. What the latch still governs is the *cue*, so that is what this asserts. The
    generalisation is worth keeping: when a fix relocates ownership, every test that used the old
    owner as its instrument is silently measuring something else."""
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())  # answers nothing, ever
    async with _rig(client=client, think_timeout_s=10.0) as rig:
        await _speak(rig, correlation_id=uuid4())
        await rig.collector.settle()

        # Turn one's reply beats its own falling edge: a delta lands BEFORE speech_ended.
        await rig.service._on_assistant_audio(
            AssistantAudioChunk(
                chunk=AudioChunk(pcm=b"\x00\x00", sample_rate=24000, channels=1),
                item_id="item_first",
            )
        )
        assert rig.service._first_audio is True
        await rig.bus.publish(
            AudioSpeechEnded(
                **envelope(clock=rig.clock, correlation_id=uuid4(), source="test"),
                duration_ms=200,
            )
        )
        await rig.collector.settle()
        # Correct: this turn's token already arrived, so no "one sec" played over it.
        assert rig.service._thinking_task is None

        # Turn two waits on a model that says nothing — the arc AVID-170/171 exists for.
        await rig.bus.publish(
            AudioSpeechEnded(
                **envelope(clock=rig.clock, correlation_id=uuid4(), source="test"),
                duration_ms=200,
            )
        )
        await rig.collector.settle()
        assert rig.service._thinking_task is not None, (
            "the latch stuck: one early reply silenced the §6.9 cue for the whole session"
        )


async def _arm_the_deadline(rig: Rig) -> None:
    """Drive a whole utterance, leaving the §6.9 deadline armed by the entry into THINKING.

    Rebuilt on :func:`_utterance` for #452: the deadline is armed by the *state* now, and the rig
    starts in THINKING without ever entering it, so publishing the two facts alone no longer arms
    anything — nor should it.

    The ``settle`` inside ``_utterance`` between the two edges is still load-bearing, but for a
    smaller reason than it used to be: the bus is FIFO **per subscriber, not across** (#72), so
    ``audio.speech_ended`` can be delivered before ``audio.speech_started`` has opened the
    session. That used to mean the deadline was never armed and every caller here passed
    *vacuously*; since #452 the deadline survives that ordering (there is a test for it), and the
    settle only keeps the session open before the falling edge, which these tests still want.

    The closing assertion is the guard against it coming back: every caller here is about what
    the deadline does, so a caller that has no deadline is a broken test, not a passing one."""
    await _utterance(rig, correlation_id=uuid4())
    assert rig.service._think_task is not None, "the deadline was never armed"


async def test_a_slow_first_token_degrades_and_tears_the_session_down() -> None:
    """AVID-171 / §6.9: a session the model never answers must not park the robot forever.

    The empty timeline *is* the bug — on hardware a 60 ms noise blip opened exactly this: a live
    socket, no first token, and 54 seconds in THINKING with no row out.

    Two assertions carry more weight than the transition itself. **No
    ``conversation.session_lost``**: nothing dropped, we gave up, and ``conversation_pi.py``
    counts that event to grade #106's AC-6 — publishing it here would make a pulled cable and a
    quiet model indistinguishable in the one artifact that grades recovery. **The session is
    torn down**: DEGRADED has no row for either ``audio.playback_*`` trigger, justified in
    ``domain/state.py`` by every path here killing the pump first, and a late delta arriving
    into DEGRADED would falsify that."""
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())  # answers nothing, ever
    async with _rig(client=client, think_timeout_s=10.0) as rig:
        await _arm_the_deadline(rig)
        await _advance_until(
            rig, lambda: rig.state.state is RobotState.DEGRADED, step_s=1.0
        )

        moves = [
            (e.from_, e.trigger, e.to)
            for e in rig.collector.of_type(StateTransitioned)
            if isinstance(e, StateTransitioned)
        ]
        assert (
            RobotState.THINKING,
            Trigger.THINK_TIMEOUT,
            RobotState.DEGRADED,
        ) in moves

        entered = rig.collector.of_type(SystemDegradedEntered)
        assert len(entered) == 1
        assert isinstance(entered[0], SystemDegradedEntered)
        assert entered[0].cause == "think_timeout"

        assert rig.collector.of_type(ConversationSessionLost) == []
        assert rig.client.closed
        assert rig.service._pump_task is None
        assert rig.service._mic_task is None
        assert any(p.name == "something_wrong.wav" for p in rig.speaker.files_played)


async def test_the_first_token_cancels_the_think_timeout() -> None:
    """The other edge: a turn that *is* answered must never degrade, however long it then runs.

    Armed at the falling edge and cancelled in the same latch that cancels the thinking cue, so
    a reply that arrives keeps the robot out of DEGRADED for the rest of the session."""
    clock = FakeClock()
    async with _rig(
        client=_replay("two_turn", clock=clock), think_timeout_s=1.0
    ) as rig:
        await _arm_the_deadline(rig)
        await _advance_until(rig, lambda: bool(rig.sink.played))

        assert rig.service._think_task is None
        await rig.clock.advance(5.0)  # past the deadline the turn was racing
        await rig.collector.settle()

        assert rig.state.state is not RobotState.DEGRADED
        assert rig.collector.of_type(SystemDegradedEntered) == []


async def test_a_delta_landing_on_the_deadline_wins_the_race() -> None:
    """The guard for the one interleaving cancellation cannot cover (§6.9, AVID-171).

    ``_cancel_task`` is fire-and-forget, and a cancel only lands at the target's next *await*.
    So a first delta arriving in the same tick the deadline expires sets ``_first_audio`` and
    requests the cancel while :meth:`_think_timer` is already past its ``sleep`` — the cancel is
    then too late and the timer runs on. The re-read of ``_first_audio`` is what keeps that from
    degrading a turn the model did in fact answer.

    Reproduced by setting the latch directly, because the race is a scheduler interleaving no
    public sequence can pin deterministically. Asserting private state is the established idiom
    here (``_first_audio``/``_thinking_task`` are asserted in the cue tests) and is the honest
    alternative to leaving a real guard uncovered."""
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())
    async with _rig(client=client, think_timeout_s=1.0) as rig:
        await _arm_the_deadline(rig)
        # The delta landed; its cancel has been requested but cannot take effect in time.
        rig.service._first_audio = True

        await rig.clock.advance(5.0)
        await rig.collector.settle()

        assert rig.state.state is not RobotState.DEGRADED
        assert rig.collector.of_type(SystemDegradedEntered) == []
        assert not rig.client.closed


async def test_a_resumed_utterance_cancels_the_think_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The user talking again ends the wait — ``(THINKING, speech_started) -> LISTENING``.

    Without the cancel at the rising edge the deadline would fire in LISTENING, which has no
    ``THINK_TIMEOUT`` row, and the only symptom would be a WARNING nobody reads. So the
    assertion is the *absence* of that warning, not just the absence of a degrade."""
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())
    with caplog.at_level(logging.WARNING, logger="avid.state"):
        async with _rig(client=client, think_timeout_s=1.0) as rig:
            await _arm_the_deadline(rig)
            # The user starts again before the model ever answered — the real edge AudioService
            # would drive, applied to the machine as well as the service.
            await _speak(rig, correlation_id=uuid4())
            await rig.state.transition(
                Trigger.AUDIO_SPEECH_STARTED, correlation_id=uuid4()
            )
            await rig.clock.advance(
                5.0
            )  # past the 1 s deadline the cancelled timer had
            await rig.collector.settle()

            assert rig.state.state is RobotState.LISTENING
            assert rig.collector.of_type(SystemDegradedEntered) == []
    assert "ignored illegal transition" not in caplog.text


async def test_the_think_timeout_does_not_fire_from_a_state_with_no_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AVID-161's overlap moves the machine out from under an armed deadline.

    A reply to an *earlier* turn draining while this one waits drives
    ``THINKING + playback_finished -> IDLE``. None of the turn-path cancel sites is reached: no
    first audio of this turn's own, no rising edge, no teardown — which is why this arc used to
    rest entirely on the in-timer state re-check.

    ⚠️ **What holds it changed with #452, and the docstring changes with it.** The arc is an
    *exit from THINKING*, so ``on_transition`` now cancels the deadline outright and the timer
    never wakes at all. That is a better answer than declining to fire, and it is asserted here
    directly — but it also means this test no longer exercises the re-check, so the same-tick
    race that guard actually covers has its own test below. A test whose stated mechanism has
    been replaced is measuring something other than what it says.

    Cancelling on ``turn_done`` instead would still be wrong: an earlier turn finishing says
    nothing about whether *this* one has been answered."""
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())
    with caplog.at_level(logging.WARNING, logger="avid.state"):
        async with _rig(client=client, think_timeout_s=1.0) as rig:
            await _arm_the_deadline(rig)
            # The earlier turn's reply drains, moving the machine out from under the deadline.
            await rig.state.transition(
                Trigger.AUDIO_PLAYBACK_FINISHED, correlation_id=uuid4()
            )
            assert rig.state.state is RobotState.IDLE

            # Past the 1 s deadline, but well short of the rig's 30 s idle close — which would
            # tear the session down for its own reasons and make the assertion below meaningless.
            await rig.clock.advance(5.0)
            await rig.collector.settle()

            assert rig.state.state is RobotState.IDLE
            assert rig.service._think_task is None, (
                "leaving THINKING left the deadline armed in a state with no row for it"
            )
            assert rig.collector.of_type(SystemDegradedEntered) == []
            assert not rig.client.closed  # the session is fine; we simply did not fire
    assert "ignored illegal transition" not in caplog.text


async def test_a_deadline_that_outlives_its_cancel_still_declines_to_fire(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The in-timer state re-check, tested for the race it actually covers (#452, AVID-161).

    ``_cancel_task`` is fire-and-forget: a cancel only lands at the target's next await, so a
    deadline expiring in the same tick it is cancelled can still reach the top of its own body.
    The re-check is what stops it driving ``THINK_TIMEOUT`` from a state with no row for it —
    only ``(THINKING, THINK_TIMEOUT)`` exists, and firing anywhere else logs the
    ``ignored illegal transition`` WARNING the M5 gate forbids.

    Driven by spawning the timer coroutine by hand, in IDLE, precisely because the observer is
    now good enough that no ordinary arc can leave one armed there. That is the honest way to
    test a guard whose whole job is to survive a race — the alternative is a test that passes
    because the race never happened."""
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())
    with caplog.at_level(logging.WARNING, logger="avid.state"):
        async with _rig(client=client, think_timeout_s=1.0) as rig:
            await _arm_the_deadline(rig)
            await rig.state.transition(
                Trigger.AUDIO_PLAYBACK_FINISHED, correlation_id=uuid4()
            )
            assert rig.state.state is RobotState.IDLE

            # A deadline the cancel did not reach in time, standing where one cannot fire.
            # The timer body, run where the cancel would have caught it a moment later. A
            # zero deadline rather than a spawned task and a clock advance, deliberately: the
            # race being modelled is one the harness must not have to *win*, and `FakeClock`
            # wakes only the sleepers an advance crosses — a version of this that spawns and
            # advances hangs instead of failing when it loses (which it did, while being
            # written).
            rig.service._think_timeout_s = 0.0
            await rig.service._think_timer()

            assert rig.state.state is RobotState.IDLE
            assert rig.collector.of_type(SystemDegradedEntered) == []
    assert "ignored illegal transition" not in caplog.text


async def test_an_idle_close_does_not_disarm_the_deadline(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The deadline belongs to the state, so a session teardown must not take it away (#452).

    ``_teardown_locked`` used to cancel it, and `Config` carries an inequality —
    ``think_timeout_s < session_idle_close_s`` — for exactly that reason: *"the idle close
    cancels the think timer and drives no transition, so a think timeout at or past it never
    fires and the robot wedges in THINKING"*. That is the #452 wedge, written down a milestone
    early and held off by a config check rather than by the code.

    This drives the ordering the check forbids, deliberately: an idle close **before** the
    deadline, with the machine still in THINKING. The socket goes; the deadline stays; the robot
    still gets out. The inequality is worth keeping as a preference — a robot that closes its
    socket mid-wait is not what anyone wants — but it is no longer the only thing standing
    between this arc and a wedge, and that is the difference this asserts."""
    clock = FakeClock()
    client = ReplayRealtimeClient(clock=clock, timeline=())  # answers nothing, ever
    with caplog.at_level(logging.WARNING, logger="avid.state"):
        async with _rig(
            client=client, session_idle_close_s=2, think_timeout_s=5.0
        ) as rig:
            await _arm_the_deadline(rig)

            # The idle close first — it tears the socket down and drives no transition at all,
            # so the machine is still in THINKING with nothing left to answer it.
            await _advance_until(rig, lambda: rig.client.closed, step_s=1.0)
            assert rig.state.state is RobotState.THINKING
            assert rig.service._session_open is False
            assert rig.service._think_task is not None, (
                "the idle close took the deadline with it — the machine is wedged again"
            )

            await _advance_until(
                rig, lambda: rig.state.state is RobotState.DEGRADED, step_s=1.0
            )

            entered = rig.collector.of_type(SystemDegradedEntered)
            assert len(entered) == 1
            assert isinstance(entered[0], SystemDegradedEntered)
            assert entered[0].cause == "think_timeout"
    assert "ignored illegal transition" not in caplog.text


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


async def _fire_trigger(rig: Rig, *, correlation_id: UUID, trigger_id: int = 7) -> None:
    """Publish ``behavior.trigger_fired`` as ``BehaviorService`` would, and let it land."""
    await rig.bus.publish(
        BehaviorTriggerFired(
            **envelope(
                clock=rig.clock, correlation_id=correlation_id, source="BehaviorService"
            ),
            trigger_id=trigger_id,
        )
    )
    await _yield(rig)


async def _yield(rig: Rig, ticks: int = 40) -> None:
    """Let the bus deliver and the opened session's tasks start, with no virtual time passing.

    Deliberately not an ``advance``: the proactive path's own hold-open timer is under test in one
    of these cases, and a drain that moved the clock would be the thing closing the session.
    """
    for _ in range(ticks):
        await asyncio.sleep(0)


# --- §10.7: the turn nobody asked for (#239) -----------------------------------------------


async def test_a_trigger_opens_a_session_and_asks_for_a_reply() -> None:
    """UC-03's mechanism. No speech, no mic audio, no committed buffer — a clock opened this."""
    clock = FakeClock()
    # An empty timeline, deliberately: the recorded fixtures replay a *user* conversation, and
    # this path is defined by the absence of one. Nothing should happen here that the trigger did
    # not cause.
    async with _rig(
        client=ReplayRealtimeClient(clock=clock, timeline=()), initial=RobotState.IDLE
    ) as rig:
        await _fire_trigger(rig, correlation_id=uuid4())

        assert rig.client.proactive_turns == 1
        assert rig.client.committed_turns == 0, (
            "§10.7: no user turn may be committed on this path"
        )
        started = [
            e for e in rig.collector.events if isinstance(e, ConversationTurnStarted)
        ]
        assert [e.initiator for e in started] == ["proactive"]


async def test_the_proactive_turn_adopts_the_triggers_correlation_id() -> None:
    """``behavior.trigger_fired`` is the head of this turn (§9.1.1) and ``BehaviorService`` minted
    the id there. Minting a second one here would split one turn into two in every log, every
    episode and every latency measurement — the one thing a correlation id exists to prevent."""
    clock = FakeClock()
    # An empty timeline, deliberately: the recorded fixtures replay a *user* conversation, and
    # this path is defined by the absence of one. Nothing should happen here that the trigger did
    # not cause.
    async with _rig(
        client=ReplayRealtimeClient(clock=clock, timeline=()), initial=RobotState.IDLE
    ) as rig:
        corr = uuid4()
        await _fire_trigger(rig, correlation_id=corr)

        started = [
            e for e in rig.collector.events if isinstance(e, ConversationTurnStarted)
        ]
        assert started and started[0].correlation_id == corr


async def test_a_proactive_turn_is_visible_to_the_episode_recorder() -> None:
    """The bug the ``_begin_turn`` extraction fixes.

    ``conversation.turn_started`` used to be published *only* from ``_on_user_transcript``, and a
    proactive turn produces no user transcript — so before #239 the turn would have opened with
    ``_turn_active`` unset, reported ``duration_ms=0`` at ``turn_ended``, and been invisible to
    ``EpisodeRecorder``. A turn the robot *chose* to have, which the transcript does not contain,
    is the worst kind to lose.
    """
    clock = FakeClock()
    # An empty timeline, deliberately: the recorded fixtures replay a *user* conversation, and
    # this path is defined by the absence of one. Nothing should happen here that the trigger did
    # not cause.
    async with _rig(
        client=ReplayRealtimeClient(clock=clock, timeline=()), initial=RobotState.IDLE
    ) as rig:
        # Move the clock off zero first: the mark is a monotonic reading, and a fresh FakeClock
        # reads 0, so "was it latched" and "is it still the sentinel" would be the same assertion.
        await rig.clock.advance(1.0)
        await _fire_trigger(rig, correlation_id=uuid4())

        assert rig.service._turn_active is True
        assert rig.service._turn_started_ns == rig.clock.monotonic_ns() > 0


async def test_a_proactive_session_closes_after_the_hold_open_window() -> None:
    """§10.7 steps 5-6: held open for a reply, then closed — on ``hold_open_s``, **not** on
    ``session_idle_close_s``.

    A reactive session is quiet because the user is thinking; a proactive one is quiet because
    nobody answered, and holding a socket open on the chance that they will is how you pay for
    silence. The idle close here is set to 600 s precisely so that a session closing on *it*
    rather than on the 30 s hold would blow the step budget and fail.
    """
    clock = FakeClock()
    async with _rig(
        client=ReplayRealtimeClient(clock=clock, timeline=()),
        initial=RobotState.IDLE,
        session_idle_close_s=600,
        hold_open_s=30.0,
    ) as rig:
        await _fire_trigger(rig, correlation_id=uuid4())
        assert rig.service._session_open is True

        await _advance_until(
            rig, lambda: not rig.service._session_open, step_s=5.0, max_steps=12
        )
        assert rig.service._session_open is False


async def test_a_trigger_arriving_mid_session_is_dropped() -> None:
    """Rule 2 should have vetoed, so reaching here means the world moved between the gate's check
    and the fire. Dropping it is right: interleaving two turns on one socket is worse than a missed
    reminder, which §10.1 already calls the cheap error."""
    clock = FakeClock()
    async with _rig(client=_replay("two_turn", clock=clock)) as rig:
        await _speak(rig, correlation_id=uuid4())
        await _yield(rig)
        assert rig.service._session_open is True

        await _fire_trigger(rig, correlation_id=uuid4())
        assert rig.client.proactive_turns == 0


# --- §10.8: what it says (#240) -------------------------------------------------------------


def test_the_context_block_matches_the_sds_example_shape() -> None:
    """AC-3: §10.8's worked example — the coffee fact at 07:55 on a Tuesday.

    The *shape*, not the wording: §10.8 is explicit that the model writes the words and that
    everything a template would carry — tone, brevity, not being annoying — already lives in §6.5's
    personality layer. What this asserts is that the block supplies the four things §10.8's example
    supplies: the time, the day, what is known about the user's presence and silence, and the fact
    that prompted the turn.
    """
    block = compose_proactive_block(
        local_time="07:55",
        weekday="Tuesday",
        present=True,
        spoken_today=False,
        fact="The user drinks coffee every day at 08:00.",
    )
    assert "07:55" in block
    assert "Tuesday" in block
    assert "present" in block
    assert "not spoken to you yet today" in block
    assert "The user drinks coffee every day at 08:00." in block
    assert "in one sentence" in block


def test_the_block_forbids_sounding_like_a_reminder_app() -> None:
    """⚠️ AC-4, and §10.8 says this clause *"is doing more work than it looks"*:

    > *"The failure mode for UC-03 isn't wrong timing; it's correct timing delivered like a calendar
    > notification. The gap between 'Good morning! Coffee time is coming soon' and 'Reminder: coffee
    > at 08:00' is the entire product."*

    Asserted rather than merely present by habit, because it is the single clause whose removal
    would leave every automated check green and the product broken.
    """
    block = compose_proactive_block(
        local_time="07:55",
        weekday="Tuesday",
        present=True,
        spoken_today=False,
        fact="coffee at 08:00",
    )
    assert "Do not sound like an alarm or a reminder app." in block


def test_the_block_is_pure_and_needs_no_model_call() -> None:
    """AC-2. The same inputs give the same text, every time, with no inference to build a prompt —
    the post-processing shape ADR-006 already ruled out for personality, ruled out here for the same
    reason: a second call to decide how to phrase the first is latency spent on something the first
    call is already good at."""
    args = {
        "local_time": "07:55",
        "weekday": "Tuesday",
        "present": True,
        "spoken_today": False,
        "fact": "coffee",
    }
    assert compose_proactive_block(**args) == compose_proactive_block(**args)  # type: ignore[arg-type]


def test_a_trigger_with_no_fact_still_composes() -> None:
    """A presence greeting (§3.7.5) has no routine behind it. The block still carries the time, the
    day and the framing — it simply has nothing specific to mention."""
    block = compose_proactive_block(
        local_time="09:10",
        weekday="Monday",
        present=True,
        spoken_today=False,
        fact=None,
    )
    assert "You know:" not in block
    assert "Do not sound like an alarm or a reminder app." in block


async def test_the_proactive_block_is_appended_after_the_memory_block() -> None:
    """§6.4's cache-prefix ordering: most-dynamic layer **last**.

    The two blocks ride the same ``memory=`` awaitable rather than a second ``open()`` kwarg —
    which would have cost a port change, three adapters and a contract suite for ordering that
    string concatenation already gives. The adapter appends whatever it is handed after the static
    layers 1-3, so the cached prefix is byte-identical on both paths.
    """
    clock = FakeClock()
    memory = _StubMemory(
        top_facts_result=(
            Fact(
                id=42,
                text="The user drinks coffee every day at 08:00.",
                kind="routine",
                importance=6,
                created_at=0,
                last_accessed_at=0,
            ),
        )
    )
    async with _rig(
        client=ReplayRealtimeClient(clock=clock, timeline=()),
        initial=RobotState.IDLE,
        memory=memory,
    ) as rig:
        await rig.bus.publish(
            BehaviorTriggerFired(
                **envelope(
                    clock=clock, correlation_id=uuid4(), source="BehaviorService"
                ),
                trigger_id=7,
                fact_id=42,
            )
        )
        await _yield(rig)

        assert rig.client.injected
        block = rig.client.injected[-1]
        assert block.index(_MEMORY_HEADER) < block.index("Do not sound like an alarm")
        assert "The user drinks coffee every day at 08:00." in block


async def test_the_block_names_the_fact_that_prompted_the_turn() -> None:
    """Given ten facts and no indication which one is due, a model picks whichever is most
    interesting rather than the one the clock fired on. Naming it is why the block restates
    something §6.7 has usually already injected a few lines above."""
    clock = FakeClock()
    memory = _StubMemory(
        top_facts_result=(
            Fact(
                id=1,
                text="The user's dog is called Biscuit.",
                kind="relationship",
                importance=7,
                created_at=0,
                last_accessed_at=0,
            ),
            Fact(
                id=42,
                text="The user drinks coffee every day at 08:00.",
                kind="routine",
                importance=6,
                created_at=0,
                last_accessed_at=0,
            ),
        )
    )
    async with _rig(
        client=ReplayRealtimeClient(clock=clock, timeline=()),
        initial=RobotState.IDLE,
        memory=memory,
    ) as rig:
        await rig.bus.publish(
            BehaviorTriggerFired(
                **envelope(
                    clock=clock, correlation_id=uuid4(), source="BehaviorService"
                ),
                trigger_id=7,
                fact_id=42,
            )
        )
        await _yield(rig)

        assert rig.client.injected
        block = rig.client.injected[-1]
        assert 'You know: "The user drinks coffee every day at 08:00."' in block
        assert 'You know: "The user\'s dog is called Biscuit."' not in block


# --- §10.8: the schedule is authoritative, the prose is not (#346) ---------------------------


def test_the_block_carries_the_scheduled_hour_not_the_one_in_the_prose() -> None:
    """⚠️ The rig said *"your 8 AM coffee ritual"* at midnight. This is the assertion that stops it.

    A routine is two rows and nothing binds them: the user's sentence in ``facts.text`` and the
    machine-readable time in ``routines.local_time``. On 2026-08-19 the schedule had been repointed
    to 00:00 and the sentence still said "8 in the morning", so the model — handed the current
    time and the prose, and nothing else — produced *"almost midnight now, your 8 AM coffee
    ritual's not too far off"*. It was not hallucinating. It was told two things and only one of
    them was true, and it had no way to tell which.

    The block now states the scheduled hour as a fact of its own, so the authoritative time is on
    the wire rather than inferred from a sentence that may have aged.
    """
    block = compose_proactive_block(
        local_time="23:55",
        weekday="Wednesday",
        present=True,
        spoken_today=False,
        fact="Ali drinks coffee every day at 8 in the morning.",
        routine_time="00:00",
    )
    assert "00:00" in block, "the schedule's own hour is missing from the block"
    assert "scheduled for 00:00" in block, (
        "the hour has to be labelled as the schedule's, or it is just a second number"
    )
    # The prose stays: it is the reason the turn is happening and §6.7 puts it on the wire anyway.
    assert "Ali drinks coffee every day at 8 in the morning." in block


def test_a_block_with_no_routine_time_is_unchanged() -> None:
    """The presence-greeting path (§3.7.5) has no routine behind it and must not gain a clause.

    Pinned rather than assumed: ``occurrence_at`` is ``None`` for a trigger with no fact, and a
    block that grew a dangling "scheduled for None" would be a regression nothing else here
    would catch.
    """
    without = compose_proactive_block(
        local_time="07:55",
        weekday="Tuesday",
        present=True,
        spoken_today=False,
        fact="The user drinks coffee every day at 08:00.",
    )
    explicit_none = compose_proactive_block(
        local_time="07:55",
        weekday="Tuesday",
        present=True,
        spoken_today=False,
        fact="The user drinks coffee every day at 08:00.",
        routine_time=None,
    )
    assert without == explicit_none
    assert "scheduled for" not in without


class _StubGesture:
    """A :class:`~avid.core.ports.GestureTools` double: records the directions asked for (#204).

    Returns ``ACCEPTED`` unconditionally — the cooldown and the no-axis decline are
    ``MotionService``'s to decide and are tested there and in the contract suite. What this
    double is for is the *seam*: that ``ConversationService`` reaches motion through a port it
    never names a service for."""

    def __init__(self) -> None:
        self.looks: list[Direction] = []

    async def look_at(
        self, direction: Direction, *, correlation_id: UUID
    ) -> LookAtResult:
        self.looks.append(direction)
        return LookAtResult.ACCEPTED
