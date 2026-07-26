"""The audio loop: VAD gate, pre-roll replay, loopback, barge-in (AVID-79, #87).

Real :class:`AsyncioEventBus`, real ``FakeMicrophone``/``FakeSpeaker``/
``FakeVoiceActivityDetector``/``FakeClock``, a real :class:`StateManager`, no mocks —
``unittest.mock`` is banned outside ``tests/adapters/`` (SDS §14.3) and would be worse
here: the properties under test are *about* dispatch, port calls and edge detection,
which a mock asserts away rather than exercises.

Turns are scripted through the VAD's per-frame timeline (``FakeVoiceActivityDetector``
returns ``script[n]`` for the n-th frame and holds the last verdict after), so a turn is
deterministic in **order** regardless of wall-clock timing — the mic paces frames on real
time, but which verdict a frame gets is fixed by its index. Waiting is always on an
:class:`asyncio.Event` (the ``_Collector``), never a sleep: the bus cancels its workers on
``stop()`` rather than draining, so "publish then sleep a bit" is a race that fails on a
loaded CI box.

Frames here are 10 ms (``chunk_ms=10`` at 16 kHz mono ⇒ 320 bytes = 32 bytes/ms), and
``silence_hold_ms=20`` ⇒ a turn ends after two consecutive silent frames — small numbers
that keep the arithmetic checkable by hand.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import NamedTuple
from uuid import uuid4

import pytest

from avid.adapters.clock import FakeClock
from avid.adapters.microphone import FakeMicrophone
from avid.adapters.speaker import FakeSpeaker
from avid.adapters.vad import FakeVoiceActivityDetector
from avid.core.event_bus import AsyncioEventBus
from avid.core.hal import AudioChunk, pcm_duration_ms
from avid.core.state_manager import StateManager
from avid.domain import (
    AudioPlaybackFinished,
    AudioPlaybackStarted,
    AudioSpeechEnded,
    AudioSpeechStarted,
    Event,
    RobotState,
    SystemHandlerFailed,
)
from avid.domain.events import REASON_HANDLER_RAISED
from avid.services.audio import _MIC_QUEUE_FRAMES, AudioService

# Generous ceiling: frames arrive every 10 ms of real time, so even a two-turn script
# (~8 frames) plus its loopback lands well inside this, while a wedged bus still fails fast.
_TIMEOUT_S = 2.0

# The four audio facts plus the bus's own failure event — everything a test asserts on.
_COLLECTED = (
    AudioSpeechStarted,
    AudioSpeechEnded,
    AudioPlaybackStarted,
    AudioPlaybackFinished,
    SystemHandlerFailed,
)

_SAMPLE_RATE = 16000
_CHANNELS = 1
_CHUNK_MS = 10
_FRAME_BYTES = _SAMPLE_RATE * _CHANNELS * 2 * _CHUNK_MS // 1000  # 320
_BYTES_PER_MS = _SAMPLE_RATE * _CHANNELS * 2 // 1000  # 32

# Assistant playback is 24 kHz mono S16_LE (§6.2.4), distinct from the 16 kHz capture rate.
_OUT_RATE = 24000


def _out_chunk(*, ms: int, fill: int) -> AudioChunk:
    """One ``ms``-long assistant PCM delta at 24 kHz — a ``play()`` argument."""
    length = _OUT_RATE * _CHANNELS * 2 * ms // 1000
    return AudioChunk(
        pcm=bytes([fill]) * length, sample_rate=_OUT_RATE, channels=_CHANNELS
    )


class _Collector:
    """Records every event of the types it is subscribed for, with an awaitable signal."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._arrived = asyncio.Event()

    async def handle(self, event: Event) -> None:
        self.events.append(event)
        self._arrived.set()

    def of_type(self, cls: type[Event]) -> list[Event]:
        return [e for e in self.events if isinstance(e, cls)]

    async def wait_for_type(self, cls: type[Event], count: int) -> None:
        """Block until at least *count* events of *cls* have arrived, or fail the test."""
        async with asyncio.timeout(_TIMEOUT_S):
            while len(self.of_type(cls)) < count:
                self._arrived.clear()
                if len(self.of_type(cls)) >= count:
                    return
                await self._arrived.wait()

    async def settle(self) -> None:
        """Let the bus drain what is queued, for the "nothing more happened" assertions."""
        for _ in range(10):
            await asyncio.sleep(0)


class Rig(NamedTuple):
    """Everything a test needs, wired the way ``main`` will wire the audio loop (#89)."""

    service: AudioService
    bus: AsyncioEventBus
    state: StateManager
    mic: FakeMicrophone
    speaker: FakeSpeaker
    vad: FakeVoiceActivityDetector
    collector: _Collector


_ExtraSub = tuple[type[Event], Callable[[Event], Awaitable[None]], str]


@contextlib.asynccontextmanager
async def _rig(
    *,
    vad_script: list[bool],
    initial: RobotState = RobotState.IDLE,
    silence_hold_ms: int = 20,
    ring_buffer_ms: int = 300,
    loopback: bool = False,
    speaker_factory: Callable[[StateManager], FakeSpeaker] | None = None,
    extra_subs: tuple[_ExtraSub, ...] = (),
) -> AsyncIterator[Rig]:
    """A started bus + running AudioService, driven by *vad_script*.

    *speaker_factory* is handed the rig's real :class:`StateManager` so a state-aware spy
    speaker (the barge-in test) observes the same state the service drives. All
    ``subscribe()`` calls precede ``bus.start()`` — the bus freezes its subscriber graph
    there — and both the service and the bus are torn down on exit.
    """
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    state = StateManager(bus=bus, clock=clock, initial=initial)
    # Explicit silence, not the default synthesized tone: the VAD is scripted (it ignores
    # the PCM) and every assertion is on byte *lengths*, so content is irrelevant — and it
    # skips FakeMicrophone's 16k-iteration synth loop, which under coverage tracing is real
    # CPU on the loop that trips the P8 slow-callback gate (it is test setup, not the path).
    mic = FakeMicrophone(
        sample_rate=_SAMPLE_RATE,
        channels=_CHANNELS,
        chunk_ms=_CHUNK_MS,
        pcm=b"\x00" * _FRAME_BYTES,
    )
    spk = speaker_factory(state) if speaker_factory is not None else FakeSpeaker()
    vad = FakeVoiceActivityDetector(script=vad_script)
    collector = _Collector()
    service = AudioService(
        bus=bus,
        clock=clock,
        state=state,
        microphone=mic,
        speaker=spk,
        vad=vad,
        ring_buffer_ms=ring_buffer_ms,
        sample_rate=_SAMPLE_RATE,
        channels=_CHANNELS,
        silence_hold_ms=silence_hold_ms,
        loopback=loopback,
    )
    for cls in _COLLECTED:
        bus.subscribe(cls, collector.handle, name=f"test.{cls.__name__}")
    for event_type, handler, name in extra_subs:
        bus.subscribe(event_type, handler, name=name)

    await bus.start()
    await service.start()
    try:
        yield Rig(service, bus, state, mic, spk, vad, collector)
    finally:
        await service.stop()
        await bus.stop()


# --- the emitted-bytes formula (AC-3), timing-free -----------------------------------------


def test_played_ms_is_bytes_over_the_chunks_own_format() -> None:
    """AC-3's "÷ 48 bytes/ms at 24 kHz mono 16-bit", and the same rule at the 16 kHz the
    loopback echoes. Format is a parameter, so one formula serves capture and playback."""
    assert pcm_duration_ms(b"\x00" * 48, sample_rate=24000, channels=1) == 1
    assert pcm_duration_ms(b"\x00" * 96, sample_rate=24000, channels=1) == 2
    assert (
        pcm_duration_ms(b"\x00" * 32, sample_rate=16000, channels=1) == 1
    )  # the loopback rate
    assert pcm_duration_ms(b"", sample_rate=24000, channels=1) == 0


# --- the service shape (SDS §9.2) ----------------------------------------------------------


async def test_audioservice_subscribes_to_nothing() -> None:
    """AC-3 drift fix: AudioService is **not** a ``conversation.*`` subscriber — the assistant
    audio seam is the ``TurnSink`` port it implements (direct calls, §9.1.4), not the bus. So
    ``subscriptions()`` is empty, permanently."""
    async with _rig(vad_script=[False]) as rig:
        assert rig.service.subscriptions() == ()
        assert rig.service.name == "AudioService"


async def test_start_is_idempotent_and_stop_finalizes_the_stream() -> None:
    """Two ``start``s spawn one task; ``stop`` cancels it and lets the mic generator's
    ``finally`` run (``closed``), and a second ``stop`` is a clean no-op."""
    async with _rig(vad_script=[True, True, False, False]) as rig:
        await rig.collector.wait_for_type(AudioSpeechStarted, 1)
        task = rig.service._task
        await rig.service.start()  # idempotent — no second task
        assert rig.service._task is task
    # The context manager's exit called stop(); the stream was finalized and re-stopping
    # is safe.
    assert rig.mic.closed
    await rig.service.stop()


# --- the loopback path (loopback=True), kept for the #91 on-Pi transport gate --------------


async def test_a_full_turn_publishes_the_four_audio_facts_on_one_correlation_id() -> (
    None
):
    """The loopback happy path end to end: two silent frames of pre-roll, three of speech, two
    of trailing silence to close it. Exactly one of each fact, all on the minted turn id. This
    is the M4 echo, retained under ``loopback=True`` for the transport gate (#91).

    ring_buffer_ms = the drained pre-roll: frames 0,1 (silence) plus frame 2 (the first
    speech frame, appended before the drain) = 3 frames x 10 ms = 30 ms. duration_ms = the
    three *speech* frames = 30 ms (the trailing silence is not speech). played_ms = the
    whole captured clip, frames 0..6 = 7 x 320 B = 2240 B over 32 B/ms = 70 ms.
    """
    script = [False, False, True, True, True, False, False]
    async with _rig(vad_script=script, loopback=True) as rig:
        await rig.collector.wait_for_type(AudioPlaybackFinished, 1)
        await rig.collector.settle()

        started = rig.collector.of_type(AudioSpeechStarted)
        ended = rig.collector.of_type(AudioSpeechEnded)
        p_started = rig.collector.of_type(AudioPlaybackStarted)
        p_finished = rig.collector.of_type(AudioPlaybackFinished)
        assert len(started) == len(ended) == len(p_started) == len(p_finished) == 1

        assert isinstance(started[0], AudioSpeechStarted)
        assert started[0].ring_buffer_ms == 30  # AC-1: pre-roll replayed
        assert isinstance(ended[0], AudioSpeechEnded)
        assert ended[0].duration_ms == 30
        assert isinstance(p_finished[0], AudioPlaybackFinished)
        assert p_finished[0].played_ms == 70
        assert p_finished[0].truncated is False  # AC-3: no truncation path at M4

        # item_id ties playback_started to playback_finished; the whole turn shares the one
        # minted correlation_id (SDS §9.1.1 — mint at origin, propagate downstream).
        assert isinstance(p_started[0], AudioPlaybackStarted)
        assert p_started[0].item_id == p_finished[0].item_id
        turn_id = started[0].correlation_id
        assert {
            e.correlation_id
            for e in (started[0], ended[0], p_started[0], p_finished[0])
        } == {turn_id}

        # AC-3: the captured clip actually reached the speaker, at the capture format.
        assert len(rig.speaker.played) == 1
        assert len(rig.speaker.played[0].pcm) == 7 * _FRAME_BYTES
        assert rig.speaker.played[0].sample_rate == _SAMPLE_RATE

        # #153 did not touch this path: loopback still *buffers* the whole clip for the echo,
        # and puts nothing up the seam (there is no AI client at M4 to receive it).
        assert rig.service._mic_out.empty()


# --- AVID-91: played_ms is the device's answer, never our own arithmetic -------------------


class _LossySpeaker(FakeSpeaker):
    """A ``FakeSpeaker`` that accepts only a fraction of what it is handed.

    Stands in for the failure the M4 gate could not see: ALSA taking nothing after an
    underrun, or a device that dropped periods. ``fraction=0`` is a mute speaker — the exact
    condition under which the harness once printed ``PASS``. Subclassing the fake is this
    file's established way to vary one behaviour (see ``_StateSpySpeaker``); no mock, which
    is banned outside ``tests/adapters/`` anyway (SDS §14.3).
    """

    def __init__(self, *, fraction: float) -> None:
        super().__init__()
        self._fraction = fraction

    async def play(self, chunk: AudioChunk) -> int:
        return int(await super().play(chunk) * self._fraction)


async def test_loopback_played_ms_is_what_the_speaker_accepted_not_what_we_sent() -> (
    None
):
    """AVID-91: the published fact reports the device's answer, halved here, not the 70 ms
    of PCM handed down. Recomputing this from the buffer is what let a mute run pass."""
    script = [False, False, True, True, True, False, False]
    async with _rig(
        vad_script=script,
        loopback=True,
        speaker_factory=lambda _state: _LossySpeaker(fraction=0.5),
    ) as rig:
        await rig.collector.wait_for_type(AudioPlaybackFinished, 1)
        await rig.collector.settle()

        finished = rig.collector.of_type(AudioPlaybackFinished)[0]
        assert isinstance(finished, AudioPlaybackFinished)
        assert finished.played_ms == 35  # half of the 70 ms submitted
        assert (
            len(rig.speaker.played[0].pcm) == 7 * _FRAME_BYTES
        )  # all of it was offered


async def test_a_mute_speaker_reports_zero_played_ms(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """**The regression for the headline defect.** A speaker that plays nothing must publish
    ``played_ms == 0``, so a gate can fail on it — and the shortfall must name the turn.

    This is the whole of AVID-91 in one assertion. The old service computed ``played_ms``
    from the length of the buffer it submitted, so a mute robot published a confident
    ``played_ms=6000`` and the gate printed ``PASS: all 3 turns within the 200 ms budget``.
    """
    script = [False, False, True, True, True, False, False]
    with caplog.at_level(logging.WARNING, logger="avid.services.audio"):
        async with _rig(
            vad_script=script,
            loopback=True,
            speaker_factory=lambda _state: _LossySpeaker(fraction=0.0),
        ) as rig:
            await rig.collector.wait_for_type(AudioPlaybackFinished, 1)
            await rig.collector.settle()

            finished = rig.collector.of_type(AudioPlaybackFinished)[0]
            assert isinstance(finished, AudioPlaybackFinished)
            assert finished.played_ms == 0
            turn_id = finished.correlation_id

    # DoD: a new failure path logs against its correlation id (SDS §3.12.2).
    assert "accepted 0 of 70 ms" in caplog.text
    assert str(turn_id) in caplog.text


async def test_speech_start_drives_idle_to_listening_without_barge_in() -> None:
    """From IDLE, a speech-start moves the machine to LISTENING and never touches the
    speaker's stop() — barge-in is only for interrupting active playback (AC-5 negative)."""
    async with _rig(
        vad_script=[True, True, False, False], initial=RobotState.IDLE
    ) as rig:
        await rig.collector.wait_for_type(AudioSpeechStarted, 1)
        assert rig.state.state is RobotState.LISTENING
        assert rig.speaker.stops == 0


# --- AC-2: debounce, and distinct turns ----------------------------------------------------


async def test_a_single_silent_frame_does_not_end_the_turn() -> None:
    """The debounce (AC-2): a one-frame dip to silence mid-utterance is a pause between
    words, not the end. Only ``silence_hold_ms`` of continuous silence closes the turn, so
    the whole script yields exactly one turn, not two."""
    script = [
        True,
        False,
        True,
        True,
        False,
        False,
    ]  # the lone False at index 1 is a gap
    async with _rig(vad_script=script, loopback=True) as rig:
        await rig.collector.wait_for_type(AudioPlaybackFinished, 1)
        await rig.collector.settle()

        assert len(rig.collector.of_type(AudioSpeechStarted)) == 1
        assert len(rig.collector.of_type(AudioSpeechEnded)) == 1


async def test_two_turns_mint_two_distinct_correlation_ids() -> None:
    """Each rising edge is a fresh turn origin, so two turns carry two different ids —
    which is what lets one grep separate them (SDS §3.12.2)."""
    script = [True, True, False, False, True, True, False, False]
    async with _rig(vad_script=script) as rig:
        await rig.collector.wait_for_type(AudioSpeechStarted, 2)

        started = rig.collector.of_type(AudioSpeechStarted)
        assert len(started) == 2
        assert started[0].correlation_id != started[1].correlation_id


# --- AC-5: local barge-in ------------------------------------------------------------------


class _StateSpySpeaker(FakeSpeaker):
    """A ``FakeSpeaker`` that records the robot state at the instant ``stop`` is called —
    so a test can prove barge-in cut playback *before* the SPEAKING -> LISTENING move."""

    def __init__(self, *, state: StateManager) -> None:
        super().__init__()
        self._state = state
        self.state_at_stop: list[RobotState] = []

    async def stop(self) -> None:
        self.state_at_stop.append(self._state.state)
        await super().stop()


async def test_barge_in_stops_the_speaker_before_transitioning_out_of_speaking() -> (
    None
):
    """AC-5: while SPEAKING, a speech-start cuts playback immediately and *then* the
    SPEAKING -> LISTENING transition lands. The spy proves the ordering: stop() saw
    SPEAKING, i.e. it ran before the machine moved."""
    async with _rig(
        vad_script=[True, True, False, False],
        initial=RobotState.SPEAKING,
        speaker_factory=lambda state: _StateSpySpeaker(state=state),
    ) as rig:
        await rig.collector.wait_for_type(AudioSpeechStarted, 1)

        assert rig.state.state is RobotState.LISTENING
        assert rig.speaker.stops == 1
        assert isinstance(rig.speaker, _StateSpySpeaker)
        assert rig.speaker.state_at_stop == [RobotState.SPEAKING]


# --- AC-7: a raising subscriber never kills the audio loop ----------------------------------


class _Raiser:
    """A subscriber that always raises — the failing display of the audio path."""

    def __init__(self) -> None:
        self.calls = 0

    async def handle(self, event: Event) -> None:
        self.calls += 1
        raise RuntimeError("subscriber blew up")


async def test_a_raising_subscriber_is_isolated_and_the_loop_keeps_running() -> None:
    """SDS §3.5.2, the single most important reliability property: a subscriber that raises
    on ``audio.speech_started`` is swallowed and republished as ``system.handler_failed``,
    and the mic loop keeps going — the *second* turn still publishes, proving it survived."""
    raiser = _Raiser()
    script = [True, True, False, False, True, True, False, False]
    async with _rig(
        vad_script=script,
        extra_subs=((AudioSpeechStarted, raiser.handle, "test.raiser"),),
    ) as rig:
        await rig.collector.wait_for_type(SystemHandlerFailed, 1)
        await rig.collector.wait_for_type(AudioSpeechStarted, 2)

        # The loop outlived the raising subscriber: two full turns landed.
        assert len(rig.collector.of_type(AudioSpeechStarted)) == 2
        assert raiser.calls >= 1

        failures = rig.collector.of_type(SystemHandlerFailed)
        assert failures
        failure = failures[0]
        assert isinstance(failure, SystemHandlerFailed)
        assert failure.handler == "test.raiser"
        assert failure.reason == REASON_HANDLER_RAISED


# --- #103: the TurnSink seam (loopback=False) ----------------------------------------------


async def _drain_mic(rig: Rig, count: int) -> list[AudioChunk]:
    """Pull *count* chunks off one ``mic()`` iterator, failing the test rather than hanging.

    One iterator, deliberately: every ``mic()`` call returns a fresh generator over the *same*
    queue, so two of them would race for frames and the ordering assertions would be a coin
    flip. That is the real seam's shape — a single consumer (``ConversationService``) drains it
    for the session's life."""
    stream = rig.service.mic()
    return [await asyncio.wait_for(anext(stream), _TIMEOUT_S) for _ in range(count)]


async def test_seam_streams_the_preroll_then_one_chunk_per_captured_frame() -> None:
    """#153/§6.3: in seam mode capture is **streamed**, not buffered — the drained pre-roll
    first, then every frame as it is captured, trailing silence included.

    The shape is the assertion, and it is timing-free. Buffering (what this service did until
    #153) produced exactly *one* chunk of 2240 B at the falling edge; streaming produces five:
    the 3-frame pre-roll (frames 0,1 + the speech frame that fired the edge), then frames 3..6
    one at a time. Same bytes, four fewer round trips of latency. Nothing is played — the echo
    is loopback's, not the seam's."""
    script = [False, False, True, True, True, False, False]
    async with _rig(vad_script=script, loopback=False) as rig:
        await rig.collector.wait_for_type(AudioSpeechEnded, 1)
        chunks = await _drain_mic(rig, 5)

        assert [len(c.pcm) for c in chunks] == [
            3 * _FRAME_BYTES,  # §6.3's ring-buffer replay, ahead of the live stream
            _FRAME_BYTES,  # frame 3, speech
            _FRAME_BYTES,  # frame 4, speech
            _FRAME_BYTES,  # frame 5, trailing silence — the server VAD needs to hear it
            _FRAME_BYTES,  # frame 6, trailing silence; the falling edge follows
        ]
        assert (
            sum(len(c.pcm) for c in chunks) == 7 * _FRAME_BYTES
        )  # frames 0..6, all of it
        assert all(
            c.sample_rate == _SAMPLE_RATE for c in chunks
        )  # 16 kHz capture, not 24
        assert all(c.channels == _CHANNELS for c in chunks)
        assert rig.speaker.played == []  # no loopback echo in seam mode
        assert (
            rig.service._mic_out.empty()
        )  # nothing withheld for a flush that never comes


async def test_seam_audio_reaches_the_model_before_the_turn_is_over() -> None:
    """#153, the regression this issue exists for: the model must hear the user *while* they
    are still speaking, or it cannot prefill and O1 is unreachable (measured P50 1350 ms vs a
    800 ms budget when this was buffered).

    Thirty speech frames give a ~300 ms window between the rising edge and the debounced
    falling one; three chunks are pulled inside it and ``audio.speech_ended`` has provably not
    been published yet. Under the old buffering the first chunk did not exist until 500 ms
    *after* the last word."""
    script = [False, False, *([True] * 30), False, False]
    async with _rig(vad_script=script, loopback=False) as rig:
        await rig.collector.wait_for_type(AudioSpeechStarted, 1)
        chunks = await _drain_mic(rig, 3)

        assert rig.collector.of_type(AudioSpeechEnded) == []  # the turn is still open
        assert len(chunks) == 3
        assert chunks[0].pcm  # the pre-roll, already on its way to the model


async def test_the_mic_queue_drops_the_oldest_frames_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#153: streaming per frame needs a bounded queue, because nothing drains it between
    sessions or while every ``open()`` is failing. Overflow keeps the **freshest** audio and
    says so — once per episode, not once per frame at 50 frames/s (§3.5.2: loud drops are a
    tuning signal, silent ones are a debugging catastrophe)."""
    async with _rig(vad_script=[False], loopback=False) as rig:
        overflow = 3
        with caplog.at_level(logging.WARNING, logger="avid.services.audio"):
            for n in range(_MIC_QUEUE_FRAMES + overflow):
                rig.service._emit(bytes([n % 256]) * _FRAME_BYTES)
                if n % 64 == 0:
                    # Setup pacing, not the path under test: filling a 500-frame queue in one
                    # synchronous burst is ~500 traced calls, which trips the 50 ms P8 gate under
                    # coverage. In the real loop these arrive one per mic frame, 20 ms apart.
                    await asyncio.sleep(0)

        assert rig.service._mic_out.qsize() == _MIC_QUEUE_FRAMES  # bounded, not leaking
        # Drained off the queue rather than through _drain_mic: 500 `wait_for` timeouts in one
        # uninterrupted step is itself a >50 ms callback under coverage, and what is under test
        # here is the queue's contents, not the iterator.
        survivors = []
        while not rig.service._mic_out.empty():
            survivors.append(rig.service._mic_out.get_nowait())
            if len(survivors) % 64 == 0:
                await asyncio.sleep(0)
        # Oldest-first eviction: frames 0..2 are gone, the newest tail survived intact.
        assert survivors[0].pcm[0] == overflow
        assert survivors[-1].pcm[0] == (_MIC_QUEUE_FRAMES + overflow - 1) % 256
        assert caplog.text.count("mic-up queue full") == 1


async def test_an_empty_frame_is_never_put_on_the_seam() -> None:
    """A drained pre-roll can be empty (a rising edge on the very first frame is still one
    frame, but ``_emit`` is called with whatever ``drain()`` returned). An empty chunk carries
    no audio and would only cost the model a round trip, so it is dropped here."""
    async with _rig(vad_script=[False], loopback=False) as rig:
        rig.service._emit(b"")
        assert rig.service._mic_out.empty()


async def test_seam_play_starts_playback_and_enters_speaking() -> None:
    """AC-2: the first assistant delta publishes ``audio.playback_started`` (tagged by item and
    the turn's minted id), drives THINKING→SPEAKING, and reaches the speaker."""
    async with _rig(
        vad_script=[False], initial=RobotState.THINKING, loopback=False
    ) as rig:
        cid = uuid4()
        rig.service._turn_id = cid  # the id AudioService minted at speech_started
        await rig.service.play(_out_chunk(ms=20, fill=0x11), item_id="item_0")
        await rig.collector.wait_for_type(AudioPlaybackStarted, 1)

        assert rig.state.state is RobotState.SPEAKING
        started = rig.collector.of_type(AudioPlaybackStarted)[0]
        assert isinstance(started, AudioPlaybackStarted)
        assert started.item_id == "item_0"
        assert started.correlation_id == cid
        assert len(rig.speaker.played) == 1  # the delta streamed to the speaker


async def test_seam_end_response_finishes_playback_and_returns_to_idle() -> None:
    """AC-2: ``end_response`` closes a multi-delta response — one started/finished pair, the two
    deltas' ms accumulated, ``truncated=False``, and SPEAKING→IDLE."""
    async with _rig(
        vad_script=[False], initial=RobotState.THINKING, loopback=False
    ) as rig:
        cid = uuid4()
        rig.service._turn_id = cid
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        await rig.service.play(_out_chunk(ms=20, fill=2), item_id="item_0")
        await rig.collector.wait_for_type(AudioPlaybackStarted, 1)
        assert rig.state.state is RobotState.SPEAKING

        await rig.service.end_response()
        await rig.collector.wait_for_type(AudioPlaybackFinished, 1)

        assert rig.state.state is RobotState.IDLE
        fin = rig.collector.of_type(AudioPlaybackFinished)[0]
        assert isinstance(fin, AudioPlaybackFinished)
        assert fin.item_id == "item_0"
        assert fin.truncated is False
        assert fin.correlation_id == cid
        assert fin.played_ms == 40  # two 20 ms deltas @ 24 kHz
        assert (
            len(rig.collector.of_type(AudioPlaybackStarted)) == 1
        )  # one pair, not two


async def test_seam_end_response_is_a_noop_when_nothing_is_playing() -> None:
    """AC-2: a turn that produced no audio → ``end_response`` publishes nothing and does not
    touch the state machine (no spurious SPEAKING→IDLE)."""
    async with _rig(
        vad_script=[False], initial=RobotState.THINKING, loopback=False
    ) as rig:
        await rig.service.end_response()
        await rig.collector.settle()
        assert rig.collector.of_type(AudioPlaybackFinished) == []
        assert rig.state.state is RobotState.THINKING


async def test_seam_interrupt_reports_played_ms_and_marks_truncated() -> None:
    """AC-1/AC-2: a barge-in ``interrupt`` returns the ms actually emitted, publishes the
    truncated ``audio.playback_finished`` fact, and is idempotent (0 with nothing playing)."""
    async with _rig(
        vad_script=[False], initial=RobotState.THINKING, loopback=False
    ) as rig:
        cid = uuid4()
        rig.service._turn_id = cid
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        await rig.collector.wait_for_type(AudioPlaybackStarted, 1)

        played = await rig.service.interrupt()
        assert played == 20
        await rig.collector.wait_for_type(AudioPlaybackFinished, 1)
        fin = rig.collector.of_type(AudioPlaybackFinished)[0]
        assert isinstance(fin, AudioPlaybackFinished)
        assert fin.truncated is True
        assert fin.played_ms == 20
        assert fin.correlation_id == cid

        # Idempotent: nothing playing now → 0, and no second fact.
        assert await rig.service.interrupt() == 0
        await rig.collector.settle()
        assert len(rig.collector.of_type(AudioPlaybackFinished)) == 1
