"""M5 gate — the permanent CI proof of the conversation loop (AVID-106, AC-1).

The runnable bench exerciser (``docs/demos/conversation_pi.py``) measures the on-Pi O1 latency and
O7 cost against the **live** Realtime API for the #106 gate; this is its in-process,
cross-platform, **network-free** counterpart that re-proves the loop's shape on every push — the
same relationship ``test_m4_gate.py`` has to ``audio_pi.py`` and ``test_m3_gate.py`` to
``face_pi.py``.

It drives the committed ``assets/sessions/`` fixtures through the **real** ``AsyncioEventBus``, the
**real** ``AudioService`` *as the ``TurnSink``* (#103 — the seam every unit test replaces with
``FakeTurnSink``) and the **real** ``ConversationService``, with fakes only and no mocks. That
full-stack wiring is what makes this an end-to-end gate rather than a second unit test: the turn
origin is minted by AudioService's own VAD gate, so ``correlation_id`` propagation is *observed*
across two services rather than injected.

Three arcs, one per gate criterion that can be proven without a network:

* **AC-2** (a conversation happens) — ``two_turn``: the four ``conversation.*`` facts land, the
  assistant PCM reaches the speaker, and every fact carries the id AudioService minted.
* **AC-3** (barge-in) — ``barge_in``: a truncating ``playback_finished`` makes the service tell the
  API the user cut in, and the cancelled sentence's in-flight audio is **dropped, not resumed**.
* **AC-6** (degrade and recover) — ``session_loss``: the drop publishes ``conversation.session_lost``,
  enters DEGRADED and plays a CueBank phrase.

Plus the part M4 taught us to write: **the harness's own pass/fail logic is under test**. A gate
that can pass on silence is not a gate, so ``_report_conversation``'s truth table is driven
directly, and one full-stack arc is re-run behind a mute speaker to prove the two halves compose —
the failure mode that let the M4 harness print ``PASS`` over a mute robot.

Drained via ``asyncio.Event`` collectors and clock advance, never a sleep, so it is stable on a
loaded CI box and on the Windows dev box. Runs clean under ``PYTHONASYNCIODEBUG=1``: the mic
streams an explicit frame (not ``FakeMicrophone``'s tone synth, which would trip the 50 ms
slow-callback gate under coverage).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest

from avid.adapters import (
    FakeClock,
    FakeMicrophone,
    FakeSpeaker,
    FakeVoiceActivityDetector,
)
from avid.adapters.realtime import ReplayRealtimeClient
from avid.core.envelope import envelope
from avid.core.event_bus import AsyncioEventBus
from avid.core.hal import AudioChunk
from avid.core.ports import Speaker
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
    RobotState,
    StateTransitioned,
    SystemDegradedEntered,
    SystemDegradedExited,
    SystemHandlerFailed,
    TokenUsage,
    Trigger,
)
from avid.services import AudioService, ConversationService, CueBank

_ROOT = Path(__file__).resolve().parents[2]
_SESSIONS = _ROOT / "assets" / "sessions"
_CUES = _ROOT / "assets" / "cues"

_SAMPLE_RATE = 16000
_CHANNELS = 1
_CHUNK_MS = 10
_FRAME_BYTES = 320  # 16000 * 1 * 2 * 10 // 1000
_SILENCE_HOLD_MS = 20
_RING_BUFFER_MS = 300

# A recognizable, non-silent frame: explicit pcm skips FakeMicrophone's tone synth, so the P8
# slow-callback gate stays quiet under coverage.
_FRAME_PCM = bytes(i % 256 for i in range(_FRAME_BYTES))
# Speech long enough to open a turn, then silence_hold + 1 frames to close it.
_SCRIPT = [True] * 3 + [False] * (_SILENCE_HOLD_MS // _CHUNK_MS + 1)

# The logger StateManager warns on when the table has no rule (SDS §3.10.3).
_STATE_LOGGER = "avid.state"

# The bench harness, loaded by path (see _load_conversation_pi) — not a package, by design.
_DEMO_MODULE = "avid_demo_conversation_pi"

_COLLECTED: tuple[type[Event], ...] = (
    ConversationTurnStarted,
    ConversationUserTranscribed,
    ConversationAssistantResponded,
    ConversationTurnEnded,
    ConversationSessionLost,
    SystemDegradedEntered,
    SystemDegradedExited,
    AudioPlaybackFinished,
    AudioSpeechStarted,
    AudioSpeechEnded,
    StateTransitioned,
    SystemHandlerFailed,
)


class _Collector:
    """Records the collected facts, with an awaitable signal — the ``test_audio.py`` rig shape."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._arrived = asyncio.Event()

    async def handle(self, event: Event) -> None:
        self.events.append(event)
        self._arrived.set()

    def of_type(self, cls: type[Event]) -> list[Event]:
        return [event for event in self.events if isinstance(event, cls)]

    async def settle(self) -> None:
        """Let queued dispatch drain, for the 'nothing more happened' assertions."""
        for _ in range(10):
            await asyncio.sleep(0)

    async def wait_for_type(
        self, cls: type[Event], count: int, *, timeout_s: float = 5.0
    ) -> None:
        """Block in **real** time until *count* events of *cls* have arrived.

        Two clocks run in this rig and they are not interchangeable. ``FakeMicrophone`` paces
        itself with ``asyncio.sleep`` — real seconds — while ``ReplayRealtimeClient`` paces its
        timeline on the injected ``FakeClock``. So the mic-driven half of the arc (frames → VAD →
        the ``audio.speech_started`` that opens a session) cannot be advanced by turning the
        virtual clock, and the replayed half cannot be advanced by waiting. Each phase gets the
        clock it actually runs on; conflating them is why the first draft of this file recorded no
        turns at all.

        Event-driven, never a sleep-poll loop (ruff ASYNC110), so it is stable on a loaded runner.
        """
        async with asyncio.timeout(timeout_s):
            while len(self.of_type(cls)) < count:
                self._arrived.clear()
                if len(self.of_type(cls)) >= count:
                    return
                await self._arrived.wait()


class _EmptyMemory:
    """An empty ``MemoryTools`` — memory is M7 (#129 has its own gate).

    ``ConversationService`` documents that empty retrieval degrades to the stateless instruction
    prefix, so this is not a stub of M5's behaviour: it *is* M5's behaviour."""

    async def remember_fact(
        self,
        text: str,
        kind: str,
        importance: int,
        *,
        correlation_id: UUID | None = None,
    ) -> int:
        return 0

    async def recall(
        self, query: str, *, k: int = 5, correlation_id: UUID | None = None
    ) -> tuple[Fact, ...]:
        return ()

    async def forget(self, query: str, *, correlation_id: UUID | None = None) -> int:
        return 0

    async def top_facts(self) -> tuple[Fact, ...]:
        return ()


class _RecordingSink:
    """A ``TurnSink`` that records assistant PCM by item — the barge-in observation point.

    The real sink is ``AudioService`` (#103) and the other arcs here use it; this one exists so
    the barge-in test can see *which* item each delta belonged to and prove the post-truncation
    ones were dropped. ``FakeTurnSink`` would do, but it cannot be handed a real speaker, and the
    arc reads better when the drop is visible in one list."""

    def __init__(self) -> None:
        self.played: list[tuple[str, AudioChunk]] = []
        self.responses_ended = 0
        self.interrupts = 0

    def mic(self) -> AsyncIterator[AudioChunk]:
        return self._mic()

    async def _mic(self) -> AsyncIterator[AudioChunk]:
        # A live mic never ends; this one yields nothing and parks, so the service's mic-forward
        # task has something cancellable to await (P8) without inventing user audio.
        if False:  # pragma: no cover - typing: makes this an async generator
            yield AudioChunk(pcm=b"", sample_rate=_SAMPLE_RATE, channels=_CHANNELS)
        await asyncio.Event().wait()

    async def play(self, chunk: AudioChunk, *, item_id: str) -> None:
        self.played.append((item_id, chunk))
        await asyncio.sleep(0)

    async def end_response(self) -> None:
        self.responses_ended += 1
        await asyncio.sleep(0)

    async def interrupt(self) -> int:
        self.interrupts += 1
        return 0


class _ScriptedVad(FakeVoiceActivityDetector):
    """A ``FakeVoiceActivityDetector`` whose timeline the test can extend **mid-run**.

    Needed because the two clocks this file documents do not commute (see
    :meth:`_Collector.wait_for_type`). A second utterance cannot simply be appended to
    ``_SCRIPT`` up front: the mic paces frames on **real** time while the replay pays out on
    **virtual** time, so a pre-scripted second utterance fires whenever the runner happens to
    get round to it — which is a race against the drop it is supposed to follow.

    Holding silence instead, and appending the next utterance only when the test asks for it,
    removes the timing assumption entirely: the fake holds its last verdict once the script runs
    out, so "silence until further notice" is its natural resting state."""

    def __init__(self) -> None:
        super().__init__(script=_SCRIPT)

    def utter(self) -> None:
        """Queue one more utterance — three speech frames, then enough silence to close it."""
        self._script.extend(_SCRIPT)


class _MuteSpeaker(FakeSpeaker):
    """Records every chunk faithfully and reports that the device took none of it.

    The mute robot, reproduced: the PCM is offered, the trace shows it, and nothing plays."""

    async def play(self, chunk: AudioChunk) -> int:
        await super().play(chunk)
        return 0


async def _advance_until(
    clock: FakeClock,
    pred: Callable[[], bool],
    *,
    step_s: float = 0.25,
    max_steps: int = 400,
) -> None:
    """Step *clock* forward until *pred* holds, yielding so dispatch can run.

    Stops the instant the predicate is satisfied, so a fixture drain never accumulates enough
    virtual time to trip the idle-close it is not exercising. Returns quietly if the predicate
    never holds — every caller asserts on the collected facts, which fail with a better message
    than a timeout would give."""
    for _ in range(max_steps):
        if pred():
            return
        await clock.advance(step_s)
        for _ in range(3):
            await asyncio.sleep(0)


async def _drive_session(
    fixture: str,
    *,
    speaker: Speaker | None = None,
    until: Callable[[_Collector], bool],
    then: Callable[[_Collector], bool] | None = None,
    barge_in_margin_db: float = 6.0,
) -> tuple[_Collector, FakeSpeaker, ReplayRealtimeClient, StateManager]:
    """Run one replayed session through the real AudioService→ConversationService stack.

    The turn origin is **not** injected: a scripted VAD timeline drives the real AudioService,
    which mints the ``correlation_id`` and publishes ``audio.speech_started``, which is what opens
    the session. AudioService is then handed to ConversationService as the ``TurnSink`` (#103) —
    the same wiring ``main._wire_services`` uses — so assistant PCM crosses the two services
    through the port and lands on a real ``FakeSpeaker``.

    *then*, when given, asks for a **second utterance** after *until* holds: the arcs that need
    one (recovery, AVID-162) have to let the first half of the fixture play out first, so the
    utterance is released rather than pre-scripted (see :class:`_ScriptedVad`).

    *barge_in_margin_db* exists for those arcs too. The mic streams one constant synthetic frame,
    so every frame sits exactly on the echo floor and AVID-159's dB margin can never be cleared —
    a second utterance offered while ``_playing_item`` is still set would be judged to be the
    robot's own echo and dropped. Setting it to 0 says *this arc is about the state machine, not
    about the discriminator*; the margin's own behaviour is unit-tested in ``test_audio.py``
    against levels that actually differ."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    state = StateManager(bus=bus, clock=clock, initial=RobotState.IDLE)
    out_speaker = speaker if speaker is not None else FakeSpeaker()
    vad = _ScriptedVad()
    audio = AudioService(
        bus=bus,
        clock=clock,
        state=state,
        microphone=FakeMicrophone(
            sample_rate=_SAMPLE_RATE,
            channels=_CHANNELS,
            chunk_ms=_CHUNK_MS,
            pcm=_FRAME_PCM,
        ),
        speaker=out_speaker,
        vad=vad,
        ring_buffer_ms=_RING_BUFFER_MS,
        sample_rate=_SAMPLE_RATE,
        channels=_CHANNELS,
        silence_hold_ms=_SILENCE_HOLD_MS,
        barge_in_margin_db=barge_in_margin_db,
        # The M5 seam: assistant PCM arrives through the TurnSink, not an M4 echo (#103).
        loopback=False,
    )
    client = ReplayRealtimeClient.from_dir(_SESSIONS / fixture, clock=clock)
    conversation = ConversationService(
        bus=bus,
        clock=clock,
        state=state,
        client=client,
        sink=audio,
        cues=CueBank(speaker=out_speaker, asset_dir=_CUES),
        memory=_EmptyMemory(),
        session_idle_close_s=30,
        memory_inject_timeout_s=1.0,
    )

    collector = _Collector()
    # Subscribe before the bus starts (P3): the service's own declared set, then the observer.
    for sub in conversation.subscriptions():
        bus.subscribe(
            sub.event_type,
            sub.handler,
            name=sub.name,
            policy=sub.policy,
            maxsize=sub.maxsize,
        )
    for cls in _COLLECTED:
        bus.subscribe(cls, collector.handle, name=f"m5_gate.{cls.__name__}")

    async with bus:
        await audio.start()
        await conversation.start()
        try:
            # Phase 1 (real time): the mic streams frames, the VAD gate closes the utterance and
            # AudioService mints the origin. Phase 2 (virtual time): the replay pays out its
            # recorded timeline. See _Collector.wait_for_type.
            #
            # Both edges are waited for, not just the rising one: the mic paces frames on **real**
            # time while ``_advance_until`` drives the replay on **virtual** time, so without this
            # nothing orders the falling edge before the reply's first delta — and since AVID-158
            # the falling edge is what carries the machine LISTENING → THINKING, where
            # ``audio.playback_started`` is legal.
            await collector.wait_for_type(AudioSpeechStarted, 1)
            await collector.wait_for_type(AudioSpeechEnded, 1)
            await collector.settle()
            await _advance_until(clock, lambda: until(collector))
            await collector.settle()
            if then is not None:
                # Act two, same alternation: release a second utterance onto the real clock,
                # wait for both of its edges, then let the replay pay out on the virtual one.
                vad.utter()
                await collector.wait_for_type(AudioSpeechStarted, 2)
                await collector.wait_for_type(AudioSpeechEnded, 2)
                await collector.settle()
                await _advance_until(clock, lambda: then(collector))
                await collector.settle()
        finally:
            await conversation.stop()
            await audio.stop()
    assert isinstance(out_speaker, FakeSpeaker)
    return collector, out_speaker, client, state


# --- AC-2: a conversation happens ----------------------------------------------------------


async def test_m5_gate_a_turn_replays_end_to_end_on_one_correlation_id() -> None:
    """AC-2: a replayed session produces the ``conversation.*`` facts, all on the one id
    AudioService minted, and the assistant's PCM reaches the speaker.

    This is the arc the two-minute on-Pi conversation demonstrates by hand. The id is *observed*
    crossing two services — AudioService mints it at ``audio.speech_started``, ConversationService
    propagates it onto every fact (SDS §3.12.2) — which is the property one ``grep`` on a live log
    depends on."""
    collector, speaker, client, _ = await _drive_session(
        "two_turn",
        until=lambda c: len(c.of_type(ConversationTurnEnded)) >= 1,
    )

    started = collector.of_type(ConversationTurnStarted)
    assert started, "the session never opened a turn"
    assert collector.of_type(ConversationUserTranscribed)
    assert collector.of_type(ConversationAssistantResponded)
    ended = collector.of_type(ConversationTurnEnded)
    assert ended

    # The origin id AudioService minted, propagated onto every conversation fact — never re-minted.
    origin = collector.of_type(AudioSpeechStarted)[0].correlation_id
    assert isinstance(origin, UUID)
    conversation_facts = [
        event
        for event in collector.events
        if isinstance(
            event,
            ConversationTurnStarted
            | ConversationUserTranscribed
            | ConversationAssistantResponded
            | ConversationTurnEnded,
        )
    ]
    assert {event.correlation_id for event in conversation_facts} == {origin}

    # AC-7's usage crossed intact, from the fixture's recorded turn_done.
    first_ended = ended[0]
    assert isinstance(first_ended, ConversationTurnEnded)
    assert first_ended.usage == TokenUsage(
        input_tokens=320, cached_input_tokens=256, output_tokens=48
    )

    # The assistant audio reached a real speaker through the TurnSink seam — not an echo.
    assert speaker.played, "no assistant PCM reached the speaker"
    assert collector.of_type(SystemHandlerFailed) == []

    # #153, at the stack level: the model was given audio *during* the user's turn, frame by
    # frame. Under the buffering this replaced, nothing crossed the seam until the falling edge,
    # so the fixture would be answering audio the model had not received. The **first** chunk is
    # §6.3's ring-buffer replay, handed over at the rising edge — before the turn was over.
    assert client.sent, "no mic audio reached the model before the turn closed"
    assert len(client.sent[0].pcm) == _FRAME_BYTES

    # AVID-158: the turn arc, and **what drove each edge**. Asserting only the shape would prove
    # nothing here — this fixture's timeline puts ``user_transcript`` before the first audio
    # delta, so the pre-fix code reaches THINKING too, just via the transcript. That ordering is
    # exactly why replay CI was structurally incapable of catching the defect: a recorded session
    # cannot reproduce a race the live API loses. Pinning the *trigger* is what makes this a
    # regression test rather than a restatement of the fixture.
    moves = [
        (e.from_, e.trigger, e.to)
        for e in collector.of_type(StateTransitioned)
        if isinstance(e, StateTransitioned)
    ]
    assert (
        RobotState.LISTENING,
        Trigger.AUDIO_SPEECH_ENDED,
        RobotState.THINKING,
    ) in moves, "the turn-end edge was not driven by AudioService's own falling edge"
    assert (
        RobotState.THINKING,
        Trigger.AUDIO_PLAYBACK_STARTED,
        RobotState.SPEAKING,
    ) in moves, "SPEAKING was never reached — the AVID-158 wedge"


# --- AC-3: barge-in ------------------------------------------------------------------------


async def test_m5_gate_barge_in_truncates_and_does_not_resume() -> None:
    """AC-3, the half CI can prove: a truncating ``playback_finished`` makes the service tell the
    API what was really heard, cancel the response, and **drop** the cancelled sentence's
    remaining audio instead of resuming it (§6.2.4 steps 5–6).

    Barge-in is split across two services (#104). AudioService owns the *local* half — VAD cuts
    the speaker and publishes ``audio.playback_finished(truncated=True)`` with the ms the device
    accepted — and that half is unit-tested in ``tests/services/test_audio.py``; the *physical*
    "the speaker stops instantly" is AC-3 on the Pi, by ear. What this proves is the seam between
    them: the fact crossing the bus drives ``truncate`` + ``cancel``, and the fixture's
    post-truncation chunk never reaches the speaker.

    The truncating fact is published directly rather than provoked through the VAD, because
    scripting a mic timeline to interrupt at a chosen moment of a clock-paced replay would be
    timing-dependent — and a flaky gate is worse than a narrow one."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    state = StateManager(bus=bus, clock=clock, initial=RobotState.LISTENING)
    speaker = FakeSpeaker()
    client = ReplayRealtimeClient.from_dir(_SESSIONS / "barge_in", clock=clock)
    sink = _RecordingSink()
    conversation = ConversationService(
        bus=bus,
        clock=clock,
        state=state,
        client=client,
        sink=sink,
        cues=CueBank(speaker=speaker, asset_dir=_CUES),
        memory=_EmptyMemory(),
        session_idle_close_s=30,
        memory_inject_timeout_s=1.0,
    )
    collector = _Collector()
    for sub in conversation.subscriptions():
        bus.subscribe(
            sub.event_type,
            sub.handler,
            name=sub.name,
            policy=sub.policy,
            maxsize=sub.maxsize,
        )
    for cls in _COLLECTED:
        bus.subscribe(cls, collector.handle, name=f"m5_gate.{cls.__name__}")

    origin = uuid4()
    async with bus:
        await conversation.start()
        try:
            await bus.publish(
                AudioSpeechStarted(
                    **envelope(clock=clock, correlation_id=origin, source="test"),
                    ring_buffer_ms=0,
                )
            )
            # Let the first assistant chunks arrive, then cut in.
            await _advance_until(clock, lambda: len(sink.played) >= 2)
            before = len(sink.played)
            await bus.publish(
                AudioPlaybackFinished(
                    **envelope(clock=clock, correlation_id=origin, source="test"),
                    item_id="item_0",
                    played_ms=640,
                    truncated=True,
                )
            )
            await collector.settle()
            await _advance_until(clock, lambda: bool(client.cancels))
            await collector.settle()
        finally:
            await conversation.stop()

    # Step 5: the API was told what the room actually heard, then the response was cancelled.
    assert client.truncations == [("item_0", 640)]
    assert client.cancels >= 1

    # Step 6: the cancelled sentence did not resume — the fixture's post-truncation chunk for
    # item_0 was dropped rather than played. This is the assertion that matters: without the
    # mute, ~200 ms of a sentence the user interrupted plays on regardless.
    assert not [item for item, _ in sink.played[before:] if item == "item_0"]
    assert collector.of_type(SystemHandlerFailed) == []


# --- AC-6: degrade and recover -------------------------------------------------------------


async def test_m5_gate_session_loss_degrades_and_plays_a_cue() -> None:
    """AC-6 (the half that needs no network): a dropped session publishes
    ``conversation.session_lost``, enters DEGRADED, and a canned CueBank phrase plays.

    The reconnect half is genuinely a network property and is proven on the Pi by pulling the
    cable (``conversation_pi.py --require-recovery``); what CI can prove is that the loss is
    *noticed*, *announced* and *audible* rather than a silent hang."""
    collector, speaker, _, state = await _drive_session(
        "session_loss",
        until=lambda c: len(c.of_type(ConversationSessionLost)) >= 1,
    )

    assert collector.of_type(ConversationSessionLost), "the drop was never published"
    assert collector.of_type(SystemDegradedEntered), "the robot never entered DEGRADED"
    assert state.state is RobotState.DEGRADED
    # The user hears something rather than silence: a cue WAV went to the speaker.
    assert speaker.files_played, "no CueBank phrase played on the drop"


async def test_m5_gate_the_recovery_turn_drives_a_whole_legal_arc(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AVID-162: the turn that *recovers* from a drop is a real turn, not a stateless one.

    Recovery is rising-edge-driven — ``_exit_degraded`` has one caller, ConversationService's
    ``audio.speech_started`` handler, after ``open()`` succeeds — so it always lands with a turn
    in flight. It used to land in IDLE, and the whole recovery turn then drove nothing: no
    thinking face, no speaking face, and no state move behind a barge-in against that reply.

    Two assertions, and the second is the one that generalises. The arc is asserted as a **whole
    journey** by ``Trigger`` — AVID-158 and AVID-161 were both cases where every row was
    defensible alone and the composition dead-ended — and then the run is required to have logged
    **no illegal transition at all**. Only the second would have caught this defect without
    knowing to look for it, and it is the reason this test lives here rather than in
    ``test_conversation.py``: illegal transitions are only reachable when the real
    ``AudioService`` is the thing driving the audio edges.

    The ``session_loss`` fixture drops *mid-playback*, so the recovery turn also exercises the
    stale-playback path: the rising edge interrupts what the drop abandoned (since AVID-158 that
    is gated on ``_playing_item``, not on ``RobotState``). That publishes an
    ``audio.playback_finished`` **fact** but drives no trigger — ``interrupt`` deliberately
    transitions nothing — which is exactly why DEGRADED needs no ``playback_*`` rows."""
    with caplog.at_level(logging.WARNING, logger=_STATE_LOGGER):
        collector, _, _, _ = await _drive_session(
            "session_loss",
            until=lambda c: len(c.of_type(SystemDegradedEntered)) >= 1,
            then=lambda c: (
                len(c.of_type(SystemDegradedExited)) >= 1
                and len(c.of_type(AudioPlaybackFinished)) >= 2
            ),
            # The synthetic frame is a constant level; see _drive_session.
            barge_in_margin_db=0.0,
        )

    assert collector.of_type(SystemDegradedExited), "the robot never recovered"

    moves = [
        (e.from_, e.trigger, e.to)
        for e in collector.of_type(StateTransitioned)
        if isinstance(e, StateTransitioned)
    ]
    lost = moves.index(
        next(m for m in moves if m[1] is Trigger.CONVERSATION_SESSION_LOST)
    )
    assert moves[lost:] == [
        # The drop, from SPEAKING — the fixture dies with a delta already on the speaker.
        (RobotState.SPEAKING, Trigger.CONVERSATION_SESSION_LOST, RobotState.DEGRADED),
        # The rising edge asks for the reopen; absorbed, because open() may still fail.
        (RobotState.DEGRADED, Trigger.AUDIO_SPEECH_STARTED, RobotState.DEGRADED),
        # It succeeded, so rejoin the turn the user is in the middle of.
        (RobotState.DEGRADED, Trigger.SYSTEM_DEGRADED_EXITED, RobotState.LISTENING),
        # ...which then runs as an ordinary turn, because it is one.
        (RobotState.LISTENING, Trigger.AUDIO_SPEECH_ENDED, RobotState.THINKING),
        (RobotState.THINKING, Trigger.AUDIO_PLAYBACK_STARTED, RobotState.SPEAKING),
        # This fixture is one recorded session replayed twice, so it drops again here. The
        # recovery turn reached SPEAKING first, which is the whole claim.
        (RobotState.SPEAKING, Trigger.CONVERSATION_SESSION_LOST, RobotState.DEGRADED),
    ], "the recovery turn did not rejoin the arc"

    assert "ignored illegal transition" not in caplog.text, (
        "the recovery turn attempted a transition the §3.10.3 table has no rule for"
    )


# --- the harness's own pass/fail logic (the M4 lesson) -------------------------------------


def _load_conversation_pi() -> ModuleType:
    """Import ``docs/demos/conversation_pi.py`` by path.

    ``docs/demos`` is not a package and is not on the path — deliberately, since these are bench
    tools rather than shipped code (they build their own object graph outside P3's composition
    root). A file loader is the zero-config way to reach it, and reaching it is the point: at M4
    the gate's pass/fail logic had no test at all, which is how it came to print PASS over a mute
    robot (AVID-91)."""
    if (cached := sys.modules.get(_DEMO_MODULE)) is not None:
        return cached
    path = _ROOT / "docs" / "demos" / "conversation_pi.py"
    spec = importlib.util.spec_from_file_location(_DEMO_MODULE, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: @dataclass resolves a slotted class's annotations through
    # sys.modules[cls.__module__], so a module loaded by path but left unregistered blows up
    # inside dataclasses, not in anything this test wrote.
    sys.modules[_DEMO_MODULE] = module
    spec.loader.exec_module(module)
    return module


def _verdict(
    demo: ModuleType,
    turns: list[Any],
    *,
    projected_usd: float = 1.0,
    check_playback: bool = True,
    live: bool = True,
    recovery: Any | None = None,
) -> int:
    """Run the demo's reporter over *turns* and return its exit code."""
    return int(
        demo._report_conversation(
            turns,
            min_turns=len(turns) or 1,
            projected_monthly_usd=projected_usd,
            cached_ratio=0.8,
            check_playback=check_playback,
            live=live,
            recovery=recovery,
        )
    )


def test_the_gate_rejects_silence_slow_turns_and_an_unaffordable_robot() -> None:
    """The reporter's truth table, driven by the numbers each gate exists to catch.

    Every row is a way the M5 gate could otherwise report success over a robot that failed:
    audio that never reached the DAC, a conversation too slow to hold, and one that meets every
    latency budget while costing more than the project is allowed to spend."""
    demo = _load_conversation_pi()

    def turn(latency: float, played: int = 2000, elapsed: float = 2000.0) -> Any:
        return demo._Turn(latency_ms=latency, played_ms=played, elapsed_ms=elapsed)

    # A healthy live conversation: comfortably inside both O1 budgets and O7.
    assert _verdict(demo, [turn(400.0), turn(600.0), turn(700.0)]) == 0
    # AVID-91: a turn the device took nothing of fails on every adapter, checked or not.
    assert _verdict(demo, [turn(400.0, played=0, elapsed=0.0)]) == 1
    assert _verdict(demo, [turn(400.0, played=0, elapsed=0.0), turn(500.0)]) == 1
    assert (
        _verdict(demo, [turn(400.0, played=0, elapsed=0.0)], check_playback=False) == 1
    )
    # O1: P50 and P95 are independently fatal.
    assert _verdict(demo, [turn(900.0), turn(950.0), turn(1000.0)]) == 1  # P50 blown
    assert _verdict(demo, [turn(400.0), turn(500.0), turn(1600.0)]) == 1  # P95 blown
    # O7: fast and mute-free, but unaffordable.
    assert _verdict(demo, [turn(400.0)], projected_usd=25.01) == 1
    assert _verdict(demo, [turn(400.0)], projected_usd=24.99) == 0
    # A wrong-rate / dropping speaker: 2000 ms "played" in 1200 ms of wall clock. The device
    # cannot emit two seconds of audio in one — that shortfall is the #146 defect.
    assert _verdict(demo, [turn(400.0, played=2000, elapsed=1200.0)]) == 1
    assert (
        _verdict(demo, [turn(400.0, played=2000, elapsed=1200.0)], check_playback=False)
        == 0
    )
    # …but the OPPOSITE direction is normal at M5 and must not fail. Assistant audio arrives as
    # streamed deltas, so wall time includes every inter-delta network gap; the real numbers are
    # turn 4 of the #106 run (1650 ms played over 3099 ms), a cold reconnect that the symmetric
    # M4-era check reported as a speaker fault.
    assert _verdict(demo, [turn(400.0, played=1650, elapsed=3099.0)]) == 0
    assert _verdict(demo, [turn(400.0, played=2000, elapsed=20000.0)]) == 0
    # No turns at all is a failure, never a vacuous pass.
    assert _verdict(demo, []) == 1


def test_a_replay_run_cannot_claim_a_live_result() -> None:
    """M5's own version of the disarmed-check defect: against ``replay`` the latencies are
    fixture-paced and the tokens are fixture literals, so the run must **withhold** O1/O7 rather
    than print a passing verdict a reader would take for a live measurement."""
    demo = _load_conversation_pi()
    turns = [demo._Turn(latency_ms=400.0, played_ms=2000, elapsed_ms=2000.0)]

    assert _verdict(demo, turns, live=True) == 0
    assert _verdict(demo, turns, live=False) == 1


def test_a_non_positive_latency_fails_rather_than_flattering_the_histogram() -> None:
    """A reply that precedes ``speech_ended`` is not a fast robot — it is a broken pairing, and
    averaging it in would drag P50 down and let a slow robot pass."""
    demo = _load_conversation_pi()
    fast = demo._Turn(latency_ms=400.0, played_ms=2000, elapsed_ms=2000.0)
    inverted = demo._Turn(latency_ms=-560.0, played_ms=2000, elapsed_ms=2000.0)

    assert _verdict(demo, [fast]) == 0
    assert _verdict(demo, [inverted]) == 1
    assert _verdict(demo, [fast, inverted, fast]) == 1


def test_recovery_is_only_gated_when_asked_and_then_both_halves_must_happen() -> None:
    """AC-6: ``--require-recovery`` demands the *whole* arc. A robot that degraded and never came
    back fails, and so does one that never noticed the drop — an unrequested check stays silent."""
    demo = _load_conversation_pi()
    turns = [demo._Turn(latency_ms=400.0, played_ms=2000, elapsed_ms=2000.0)]

    def recovery(lost: int, entered: int, exited: int) -> Any:
        return demo._Recovery(lost=lost, entered=entered, exited=exited, downtime_s=3.0)

    assert _verdict(demo, turns, recovery=None) == 0  # not asked for
    assert _verdict(demo, turns, recovery=recovery(1, 1, 1)) == 0  # full arc
    assert _verdict(demo, turns, recovery=recovery(0, 0, 0)) == 1  # never dropped
    assert _verdict(demo, turns, recovery=recovery(1, 1, 0)) == 1  # never came back


def test_a_disarmed_playback_check_says_so_out_loud(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A check that quietly skips is the defect wearing a hat, so the PASS names its scope."""
    demo = _load_conversation_pi()
    turns = [demo._Turn(latency_ms=400.0, played_ms=2000, elapsed_ms=0.0)]

    assert _verdict(demo, turns, check_playback=False) == 0
    assert "NOT CHECKED" in capsys.readouterr().out


async def test_a_mute_conversation_fails_the_gate() -> None:
    """End to end: a speaker that plays nothing publishes ``played_ms == 0``, and the bench
    harness's own reporter turns that into a **failing** exit code.

    This is the composition the M4 gate was missing, re-proven for M5. Each half is individually
    fine — the service publishes a number, the harness checks a latency — and between them a mute
    robot scored a clean PASS. Asserting the two halves *together* is what makes that impossible,
    and M5 is where it matters most: every reply is now the model's voice, not an echo the
    operator already knows the sound of."""
    collector, speaker, _, _ = await _drive_session(
        "two_turn",
        speaker=_MuteSpeaker(),
        until=lambda c: len(c.of_type(AudioPlaybackFinished)) >= 1,
    )

    finished = collector.of_type(AudioPlaybackFinished)
    assert finished, "playback never finished"
    first = finished[0]
    assert isinstance(first, AudioPlaybackFinished)
    assert first.played_ms == 0
    assert speaker.played, (
        "the speaker was offered audio"
    )  # it was offered, and took none

    demo = _load_conversation_pi()
    turn = demo._Turn(latency_ms=400.0, played_ms=first.played_ms, elapsed_ms=0.0)
    assert _verdict(demo, [turn]) == 1
