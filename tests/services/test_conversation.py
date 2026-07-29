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
from avid.core.realtime import ToolCallRequested, TurnDone, UserTranscript
from avid.core.state_manager import StateManager
from avid.domain import (
    AudioPlaybackFinished,
    AudioSpeechEnded,
    AudioSpeechStarted,
    ConversationAssistantResponded,
    ConversationSessionLost,
    ConversationTurnEnded,
    ConversationTurnStarted,
    ConversationUserTranscribed,
    Event,
    Fact,
    MemoryFactStored,
    RobotState,
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
) -> AsyncIterator[Rig]:
    """A started bus + running ConversationService driven by *client*'s recorded session.

    ``initial`` defaults to THINKING: by the time ConvSvc's pump sees a turn, AudioService has
    already driven IDLE→LISTENING on its rising edge **and** LISTENING→THINKING on its falling
    one (AVID-158), so THINKING is where a model-side turn actually begins. All ``subscribe()``
    calls precede ``bus.start()``; the service and bus are torn down on exit.
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
        session_idle_close_s=session_idle_close_s,
        memory_inject_timeout_s=memory_inject_timeout_s,
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
    """AC-1/AC-2: name plus the two ``audio.*`` origins that exist at M5 and the
    ``audio.playback_finished`` barge-in feed (#104, SDS §9.1.3). The ``behavior.trigger_fired``
    origin has no Event type yet — it is an M6 seam."""
    clock = FakeClock()
    async with _rig(client=_replay("two_turn", clock=clock)) as rig:
        assert rig.service.name == "ConversationService"
        subs = rig.service.subscriptions()
        assert {s.event_type for s in subs} == {
            AudioSpeechStarted,
            AudioSpeechEnded,
            AudioPlaybackFinished,
        }
        assert {s.name for s in subs} == {
            "ConversationService.speech_started",
            "ConversationService.speech_ended",
            "ConversationService.playback_finished",
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
            lambda: any(
                p.name == "thinking_one_sec.wav" for p in rig.speaker.files_played
            ),
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
