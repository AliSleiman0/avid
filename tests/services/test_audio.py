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
import json
import logging
import wave
from array import array
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
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
    ADMISSION_RULES,
    ECHO_FLOOR,
    ECHO_TAIL,
    REACTIVE_BUDGET,
    Admitted,
    AudioCaptureResumed,
    AudioCaptureStalled,
    AudioPlaybackFinished,
    AudioPlaybackStarted,
    AudioSpeechEnded,
    AudioSpeechStarted,
    Event,
    Refused,
    RobotState,
    SystemHandlerFailed,
    rms_dbfs,
)
from avid.domain.audio import SILENCE_DBFS
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


def _read_ambient() -> bytes:
    """The committed empty-room recording AVID-283 was filed from (5 s, 16 kHz mono)."""
    path = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "assets"
        / "audio"
        / "ambient_hum_5s.wav"
    )
    with wave.open(str(path), "rb") as handle:
        return handle.readframes(handle.getnframes())


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
    clock: FakeClock


_ExtraSub = tuple[type[Event], Callable[[Event], Awaitable[None]], str]


@contextlib.asynccontextmanager
async def _rig(
    *,
    vad_script: list[bool],
    initial: RobotState = RobotState.IDLE,
    silence_hold_ms: int = 20,
    ring_buffer_ms: int = 300,
    barge_in_margin_db: float = 6.0,
    highpass_hz: float = 150.0,
    highpass_order: int = 3,
    mic_pcm: bytes | None = None,
    echo_tail_ms: int = 250,
    guard_window_ms: int = 700,
    reactive_window_s: float = 120.0,
    reactive_back_to_back_s: float = 1.5,
    reactive_budget: int = 4,
    capture_stall_s: float = 5.0,
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
        # *mic_pcm* lets a test stream real recorded audio (AVID-283's ambient fixture) instead
        # of silence. Still explicit rather than FakeMicrophone's default tone, for the reason
        # above — a caller supplies bytes, nothing synthesises them on the loop.
        pcm=mic_pcm if mic_pcm is not None else b"\x00" * _FRAME_BYTES,
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
        barge_in_margin_db=barge_in_margin_db,
        highpass_hz=highpass_hz,
        highpass_order=highpass_order,
        echo_tail_ms=echo_tail_ms,
        guard_window_ms=guard_window_ms,
        reactive_window_s=reactive_window_s,
        reactive_back_to_back_s=reactive_back_to_back_s,
        reactive_budget=reactive_budget,
        capture_stall_s=capture_stall_s,
        loopback=loopback,
    )
    for cls in _COLLECTED:
        bus.subscribe(cls, collector.handle, name=f"test.{cls.__name__}")
    for event_type, handler, name in extra_subs:
        bus.subscribe(event_type, handler, name=name)

    await bus.start()
    await service.start()
    try:
        yield Rig(service, bus, state, mic, spk, vad, collector, clock)
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
    speaker's stop() — barge-in is only for interrupting active playback (AC-5 negative).

    ``[True]`` holds forever (the fake repeats its last verdict), so no falling edge can fire
    and carry the machine on to THINKING while this asserts on LISTENING (AVID-158)."""
    async with _rig(vad_script=[True], initial=RobotState.IDLE) as rig:
        await rig.collector.wait_for_type(AudioSpeechStarted, 1)
        assert rig.state.state is RobotState.LISTENING
        assert rig.speaker.stops == 0


# --- AC-2: debounce, and distinct turns ----------------------------------------------------


async def test_a_turn_reaches_speaking_with_no_transcript_at_all() -> None:
    """AVID-158, the regression. The machine must reach SPEAKING from this service's **own**
    audio edges alone — rising edge, falling edge, first assistant delta — with no
    ``conversation.*`` event anywhere in the path.

    Before the fix the falling edge left the machine in LISTENING, where
    ``audio.playback_started`` is illegal, so SPEAKING was unreachable on hardware and
    ``_begin_speech``'s barge-in guard was dead code. The edge hung off
    ``conversation.user_transcribed`` — a separate transcription pass that the bench measured
    arriving 1.1 s *after* the assistant had started speaking
    (``docs/demos/m5_evidence/trace_2026-07-26_streaming.log``)."""
    script = [False, False, True, True, True, False, False]
    async with _rig(vad_script=script, initial=RobotState.IDLE) as rig:
        await rig.collector.wait_for_type(AudioSpeechStarted, 1)
        await rig.collector.wait_for_type(AudioSpeechEnded, 1)
        assert rig.state.state is RobotState.THINKING  # the corrected turn-end edge

        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        await rig.collector.wait_for_type(AudioPlaybackStarted, 1)
        assert rig.state.state is RobotState.SPEAKING  # the wedge, gone

        assert rig.collector.of_type(SystemHandlerFailed) == []


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


class _ParkingSpeaker(FakeSpeaker):
    """A ``FakeSpeaker`` with the suspension points the real adapter has and the stock fake lacks.

    Two gaps, and between them they are why AVID-174 survived CI while crashing the Pi:

    * ``FakeSpeaker.play`` returns the **full** duration whatever a concurrent ``stop()`` did, so
      ``accepted_ms < submitted_ms`` — the branch that dereferences the cleared episode — is never
      true in CI. ``AlsaSpeaker._write_all`` breaks out of its period loop the moment ``stop()``
      sets its flag, and reports what it actually wrote.
    * ``FakeSpeaker.stop`` has **no await at all**, so a barge-in is atomic with respect to the
      pump and the interleaving cannot even be expressed. ``AlsaSpeaker.stop`` hops a thread.

    This fake parks inside ``play`` on an injected gate, so a test can land a barge-in squarely in
    the window the real device leaves open, and reports a truncated write when it was stopped while
    parked. Subclassing the fake is this file's established way to vary one behaviour
    (``_LossySpeaker``, ``_StateSpySpeaker``); no mock, which is banned outside ``tests/adapters/``
    anyway (SDS §14.3).
    """

    def __init__(self, *, park_first: int = 1) -> None:
        super().__init__()
        # Set once play() has parked, so the test knows the window is open without sleeping.
        self.entered = asyncio.Event()
        # The test sets this to let the parked play() finish.
        self.release = asyncio.Event()
        # Only the first *park_first* writes park; later ones run straight through, so a test can
        # open a second episode while the first write is still suspended.
        self._park_first = park_first
        self._calls = 0
        self._stopped_while_parked = False

    async def play(self, chunk: AudioChunk) -> int:
        submitted = await super().play(chunk)
        self._calls += 1
        if self._calls > self._park_first:
            return submitted
        self.entered.set()
        await self.release.wait()
        if self._stopped_while_parked:
            self._stopped_while_parked = False
            # A partial write, which is what the real device reports: ``AlsaSpeaker._write_all``
            # breaks out of its period loop when ``stop()`` sets the flag and returns the ms it
            # had already handed over. Returning 0 here would be convenient and dishonest — it
            # would hide the ms-leak half of AVID-174 rather than expose it.
            return submitted // 2
        return submitted

    async def stop(self) -> None:
        if self.entered.is_set() and not self.release.is_set():
            self._stopped_while_parked = True
        await super().stop()


async def test_a_barge_in_during_a_write_does_not_kill_the_pump() -> None:
    """AVID-174: the crash. A barge-in landing inside an in-flight ``speaker.play()``.

    ``play`` runs on ConversationService's pump task; ``interrupt`` runs on AudioService's own mic
    loop. They share the playback episode with no synchronisation, so the barge-in clears
    ``_playing_item``/``_playing_ms``/``_playing_corr`` out from under the suspended writer. The
    writer then resumes, finds the write was cut short, and dereferences the episode that no longer
    exists — ``AssertionError`` in ``_playback_corr``, on the **pump**, which is an owned task
    nothing observes. The conversation ends there: socket open, robot deaf, log silent.

    Driven with the play in its own task so the two really interleave, which is the only way to
    reproduce it — every existing barge-in test awaits ``play`` to completion first."""
    speaker = _ParkingSpeaker()
    async with _rig(
        vad_script=[False],  # the gate fires no edge of its own; we drive it
        initial=RobotState.THINKING,
        speaker_factory=lambda _state: speaker,
    ) as rig:
        rig.service._turn_id = uuid4()
        playing = asyncio.create_task(
            rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0"),
            name="test.play",
        )
        try:
            await speaker.entered.wait()  # the write is in flight
            await rig.service._begin_speech()  # the barge-in, on the other task
            speaker.release.set()
            await playing  # must not raise
        finally:
            speaker.release.set()
            if not playing.done():
                playing.cancel()

        finished = [
            e
            for e in rig.collector.of_type(AudioPlaybackFinished)
            if isinstance(e, AudioPlaybackFinished)
        ]
        assert len(finished) == 1, "the episode was finalized more than once"
        assert finished[0].truncated is True
        assert rig.service._playing_item is None
        assert rig.service._playing_ms == 0, (
            "ms from a write that outlived its episode leaked back into the service"
        )


async def test_ms_from_an_interrupted_write_never_leak_into_the_next_reply() -> None:
    """The silent half of AVID-174, which is worse than the crash because nothing reports it.

    A write that outlives its episode still runs ``self._playing_ms += accepted_ms`` when it
    resumes. If the *next* reply has already opened by then, those ms are added to a turn that
    never played them — and ``played_ms`` is exactly what ``interrupt`` returns as the model's
    ``audio_end_ms`` (§6.2.4). The model would be told the user heard audio from a previous turn.

    This is the test that pins the **epoch check specifically**: the crash in T1 is prevented by
    capturing the correlation id alone, so without this one a fix could drop the epoch entirely
    and still look green."""
    speaker = _ParkingSpeaker()
    async with _rig(
        vad_script=[False],
        initial=RobotState.THINKING,
        speaker_factory=lambda _state: speaker,
    ) as rig:
        rig.service._turn_id = uuid4()
        playing = asyncio.create_task(
            rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0"),
            name="test.play",
        )
        try:
            await speaker.entered.wait()
            await rig.service._begin_speech()  # barge-in ends episode 1
            # Episode 2 opens and plays a full 20 ms while write #1 is still suspended.
            rig.service._turn_id = uuid4()
            await rig.service.play(_out_chunk(ms=20, fill=2), item_id="item_1")
            assert rig.service._playing_ms == 20
            speaker.release.set()
            await playing  # write #1 resumes into a world that moved on
        finally:
            speaker.release.set()
            if not playing.done():
                playing.cancel()

        assert rig.service._playing_ms == 20, (
            "the interrupted write added its ms to the NEXT reply's total, so the model would "
            "be told the user heard audio from a turn that had already been cut off"
        )


async def test_a_barge_in_shortfall_is_not_reported_as_a_device_drop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A short accept because the barge-in cut the write is expected, not AVID-91's defect.

    Before AVID-174 this WARNed on **every** barge-in, because ``Speaker.stop`` closes the handle
    and the interrupted write necessarily reports less than it was handed. A warning that fires on
    correct behaviour is a warning nobody reads — and AVID-91's real signal, a device silently
    dropping audio, would drown in it."""
    speaker = _ParkingSpeaker()
    async with _rig(
        vad_script=[False],
        initial=RobotState.THINKING,
        speaker_factory=lambda _state: speaker,
    ) as rig:
        rig.service._turn_id = uuid4()
        with caplog.at_level(logging.DEBUG, logger="avid.services.audio"):
            playing = asyncio.create_task(
                rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0"),
                name="test.play",
            )
            try:
                await speaker.entered.wait()
                await rig.service._begin_speech()
                speaker.release.set()
                await playing
            finally:
                speaker.release.set()
                if not playing.done():
                    playing.cancel()

        assert "barge-in truncated the write" in caplog.text
        assert "speaker accepted" not in caplog.text, (
            "a barge-in was reported as a device drop"
        )


async def test_a_live_episode_shortfall_is_still_a_device_drop_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other half of the split: with no barge-in, a short accept still WARNs (AVID-91).

    This is the path the M4 gate's mute robot took, and until now the ``play`` seam's copy of that
    warning had no test at all — only the loopback one did."""
    async with _rig(
        vad_script=[False],
        initial=RobotState.THINKING,
        speaker_factory=lambda _state: _LossySpeaker(fraction=0.5),
    ) as rig:
        corr = uuid4()
        rig.service._turn_id = corr
        with caplog.at_level(logging.WARNING, logger="avid.services.audio"):
            await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")

        assert "speaker accepted 10 of 20 ms for item item_0" in caplog.text
        assert str(corr) in caplog.text, "the shortfall was not attributed to its turn"


async def test_end_response_and_a_barge_in_cannot_both_finalize_one_episode() -> None:
    """AVID-174 site (c): the two finalizers are mutually exclusive, under a race as well as not.

    ``end_response`` and ``interrupt`` both used to check ``_playing_item is not None`` and then
    await, so both could pass the guard for the *same* episode. That publishes a second
    ``audio.playback_finished`` — making ConversationService send ``truncate`` + ``cancel`` for a
    response that ended normally — and runs the echo-gate report twice, silently halving the
    suppression counts #106's AC-3 is calibrated from.

    ⚠️ Reaching it needs ``StateManager.transition`` to genuinely suspend, which it only does when
    its lock is contended: ``AsyncioEventBus.publish`` never yields (its body is a synchronous
    enqueue). So the lock is held from a helper task to force the window open. Without that this
    test would pass against the unfixed code and prove nothing."""
    async with _rig(vad_script=[False], initial=RobotState.THINKING) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        assert rig.state.state is RobotState.SPEAKING

        # Hold the state lock so end_response() parks inside its transition, mid-finalize.
        await rig.state._lock.acquire()
        ending = asyncio.create_task(rig.service.end_response(), name="test.end")
        try:
            await asyncio.sleep(0)
            # The barge-in arrives while end_response is suspended on the lock.
            interrupted_ms = await rig.service.interrupt()
        finally:
            rig.state._lock.release()
            await ending

        finished = [
            e
            for e in rig.collector.of_type(AudioPlaybackFinished)
            if isinstance(e, AudioPlaybackFinished)
        ]
        assert len(finished) == 1, "the episode was finalized twice"
        assert finished[0].truncated is False, (
            "end_response took it first, so it is not truncated"
        )
        assert interrupted_ms == 0, "the barge-in re-finalized an episode already taken"


async def test_a_barge_in_while_announcing_playback_abandons_the_write() -> None:
    """AVID-174 site (b): the barge-in lands between opening the episode and writing to it.

    ``play`` publishes ``audio.playback_started`` and drives ``THINKING → SPEAKING`` before it
    touches the speaker. A barge-in inside *that* window leaves the episode already taken, so the
    write must be abandoned rather than sent to a speaker that was just stopped.

    Like the finalizer test, reaching it needs the state lock contended — ``publish`` never yields
    (``test_publish_does_not_suspend_the_caller``), so the transition is the only real suspension
    point in the announce sequence."""
    async with _rig(vad_script=[False], initial=RobotState.THINKING) as rig:
        rig.service._turn_id = uuid4()
        await rig.state._lock.acquire()
        playing = asyncio.create_task(
            rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0"),
            name="test.play",
        )
        try:
            await asyncio.sleep(0)  # play() opens the episode and parks in transition
            interrupted_ms = await rig.service.interrupt()
        finally:
            rig.state._lock.release()
            await playing

        assert interrupted_ms == 0, "nothing had been written when the barge-in landed"
        assert rig.speaker.played == [], (
            "the write went to a speaker that was already stopped"
        )
        assert rig.service._playing_item is None


async def test_barge_in_stops_the_speaker_before_transitioning_out_of_speaking() -> (
    None
):
    """AC-5: while SPEAKING, a speech-start cuts playback immediately and *then* the
    SPEAKING -> LISTENING transition lands. The spy proves the ordering: stop() saw
    SPEAKING, i.e. it ran before the machine moved.

    SPEAKING is now reached the way hardware reaches it — THINKING plus a first assistant delta
    (AVID-158) — instead of being injected as ``initial``, and the rising edge is driven
    explicitly so the assertions do not race the free-running mic loop's own falling edge."""
    async with _rig(
        vad_script=[False],  # the gate fires no edge of its own
        initial=RobotState.THINKING,
        speaker_factory=lambda state: _StateSpySpeaker(state=state),
    ) as rig:
        rig.service._turn_id = uuid4()  # the turn AudioService would have minted
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        assert rig.state.state is RobotState.SPEAKING

        await rig.service._begin_speech()  # the rising edge the VAD gate fires

        assert rig.state.state is RobotState.LISTENING
        assert rig.speaker.stops == 1
        assert isinstance(rig.speaker, _StateSpySpeaker)
        assert rig.speaker.state_at_stop == [RobotState.SPEAKING]


async def test_barge_in_is_gated_on_live_playback_not_on_the_state_machine() -> None:
    """AVID-158: the interrupt fires whenever assistant audio is in flight, whatever the state
    machine believes.

    The bench measured a reply to an earlier commit beginning 0.9 s *before* our falling edge.
    Gating barge-in on ``state is SPEAKING`` made the interrupt dead code in exactly that case —
    and silently dropped the truncated ``audio.playback_finished`` the model half of §6.2.4
    depends on.

    Driven from a state the machine still refuses to move out of on ``audio.playback_started``:
    the property under test is *"the speaker is live and the machine disagrees"*, so it needs one
    to exist. That the set of such states keeps shrinking is the point of AVID-161; that the guard
    does not care is the point of this test.

    ⚠️ It shrank again at **AVID-189**, which rooted ``(IDLE, playback_started) -> SPEAKING`` after
    the M6 gate run observed a reply starting with the machine already idle. This test was driven
    from IDLE until then — and the paragraph above predicted exactly that, which is why moving it
    is a one-line change rather than an argument.

    SLEEPING, not DEGRADED: §3.10.3 justifies DEGRADED having no playback rows on the grounds that
    every path into it tears the pump down first, so a playback edge there is genuinely unreachable
    and testing it would prove nothing about a live speaker."""
    async with _rig(vad_script=[False], initial=RobotState.SLEEPING) as rig:
        rig.service._turn_id = uuid4()
        # Playback opens from SLEEPING: the transition is illegal and logged-and-ignored, so the
        # machine never reaches SPEAKING — but the speaker is live either way.
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        assert rig.state.state is RobotState.SLEEPING

        await rig.service._begin_speech()
        await rig.collector.wait_for_type(AudioPlaybackFinished, 1)

        assert rig.speaker.stops == 1
        finished = rig.collector.of_type(AudioPlaybackFinished)
        assert [e.truncated for e in finished] == [
            True
        ]  # the model half's §6.2.4 input


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


# --- AVID-159: the echo gate ---------------------------------------------------------------


async def test_captured_frames_do_not_reach_the_uplink_while_the_robot_speaks() -> None:
    """AVID-159, the regression. The model must never be fed the robot's own voice.

    At the M5 bench the mic heard the speaker 269 ms into every reply and — because #153
    streams live — forwarded it to the API as user input. The server's turn detection saw
    near-continuous audio and stopped committing turns altogether: the conversation died with
    the socket still open, while ``sent`` kept climbing.

    Driven through ``_capture`` rather than the free-running mic loop so the window under test
    is exact rather than a race: the claim is about the seam, and the seam is this call."""
    async with _rig(vad_script=[False]) as rig:
        rig.service._turn_id = uuid4()

        rig.service._capture(b"\x11" * _FRAME_BYTES)  # nothing playing — flows
        assert rig.service._mic_out.qsize() == 1

        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        rig.service._capture(b"\x22" * _FRAME_BYTES)  # the robot is talking — dropped
        rig.service._capture(b"\x33" * _FRAME_BYTES)

        assert rig.service._mic_out.qsize() == 1  # still just the pre-playback frame


async def test_the_uplink_stays_shut_for_the_echo_tail_then_reopens() -> None:
    """``end_response`` means the model has finished *sending*, not that the room has gone
    quiet: the DAC is still clocking out up to a playback-buffer depth (§6.2.4). Those frames
    are still the robot, so the uplink holds shut over ``[gate] echo_tail_ms``."""
    async with _rig(vad_script=[False], echo_tail_ms=250) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        await rig.service.end_response()

        rig.service._capture(
            b"\x22" * _FRAME_BYTES
        )  # inside the tail — still the robot
        assert rig.service._mic_out.qsize() == 0

        await rig.clock.advance(0.3)  # past the 250 ms tail
        rig.service._capture(b"\x33" * _FRAME_BYTES)
        assert rig.service._mic_out.qsize() == 1


async def test_a_barge_in_reopens_the_uplink_immediately_with_no_tail() -> None:
    """No tail after an interrupt, and the distinction matters both ways: ``Speaker.stop``
    closes the handle so ALSA drops the buffered audio (there is no drain to wait out), and the
    user is *mid-utterance* — holding the uplink shut here would clip the very words that
    interrupted."""
    async with _rig(vad_script=[False]) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        await rig.service.interrupt()

        rig.service._capture(b"\x22" * _FRAME_BYTES)
        assert (
            rig.service._mic_out.qsize() == 1
        )  # open at once, no clock advance needed


async def test_a_rising_edge_no_louder_than_the_echo_is_suppressed() -> None:
    """The robot's own voice is speech, and Silero rightly says so. Loudness is the only
    discriminator left, and a frame level with the room is our own speaker.

    The mic here emits digital silence, so the floor converges exactly on the frame level and
    nothing can clear a 6 dB margin — the "this is the echo" case, by construction."""
    async with _rig(vad_script=[False]) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")

        verdict = rig.service._admission(rms_dbfs(b"\x00" * _FRAME_BYTES))
        assert isinstance(verdict, Refused)
        assert verdict.rule == ECHO_FLOOR
        assert rig.service._suppressed_frames == 1
        assert rig.service.admission_refusals() == {ECHO_FLOOR: 1}


async def test_a_rising_edge_that_clears_the_margin_barges_in() -> None:
    """AC-5 survives the gate: loud enough is the user, and the whole §6.2.4 chain runs."""
    async with _rig(vad_script=[False], barge_in_margin_db=0.0) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")

        verdict = rig.service._admission(rms_dbfs(b"\x00" * _FRAME_BYTES))
        assert isinstance(verdict, Admitted)
        assert verdict.tested, "a frame judged while the robot spoke was not tested"
        assert rig.service._suppressed_frames == 0


async def test_normal_turn_taking_is_never_tested_against_the_margin() -> None:
    """The gate's blast radius, pinned: the margin only ever judges a rising edge that happens
    while the robot is speaking. With a silent speaker every edge is the user's, whatever the
    margin — so an impossible margin cannot make the robot deaf in ordinary conversation."""
    async with _rig(vad_script=[False], barge_in_margin_db=999.0) as rig:
        verdict = rig.service._admission(rms_dbfs(b"\x00" * _FRAME_BYTES))
        assert isinstance(verdict, Admitted)
        # Stronger than "admitted": nothing was even consulted. The two used to be
        # indistinguishable, and that is exactly how #467 hid for eight minutes.
        assert not verdict.tested
        assert rig.service._suppressed_frames == 0
        assert rig.service.admission_refusals() == {}


async def test_a_suppressed_edge_neither_mints_a_turn_nor_publishes() -> None:
    """A suppressed edge is a non-event: no origin minted, no fact published, no interrupt.
    Driven through the real mic loop, so this is ``_run`` consulting the gate, not the helper
    in isolation — the default silent frames can never clear the 6 dB margin."""
    async with _rig(vad_script=[False] * 4 + [True]) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        await rig.collector.wait_for_type(AudioPlaybackStarted, 1)

        with pytest.raises(TimeoutError):  # the edge never becomes a turn
            await asyncio.wait_for(
                rig.collector.wait_for_type(AudioSpeechStarted, 1), 0.3
            )

        assert rig.speaker.stops == 0  # the robot did not cut itself off
        assert rig.service._suppressed_frames > 0


async def test_the_echo_gate_reports_its_calibration_on_every_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#106's AC-3 requires the margin to be a *measured* number rather than a hidden constant,
    so every playback episode prints the floor it saw and the closest any rejected edge came to
    clearing it. Every bench run is therefore a calibration run, with no separate mode to
    remember — and if those levels never separate from a genuine barge-in's, that is the signal
    to stop tuning and take #163 (AEC) instead."""
    with caplog.at_level(logging.INFO, logger="avid.services.audio"):
        async with _rig(vad_script=[False]) as rig:
            rig.service._turn_id = uuid4()
            await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
            rig.service._admission(rms_dbfs(b"\x00" * _FRAME_BYTES))
            await rig.service.end_response()

    assert "1 suppressed" in caplog.text
    assert "margin 6.0 dB" in caplog.text
    # AVID-283 AC-5: the line reports the FILTERED floor, and names the filter that produced it.
    # A floor logged before the high-pass and one logged after are not comparable — they differ
    # by ~31 dB of energy no human produced — so a bench log has to say which it is, or an old
    # number and a new one silently look like a regression. And the filter is read from the
    # service's own configured instance, never restated: a banner quoting a value the run did not
    # use is drift with a delay fuse (CLAUDE.md §7.1).
    assert "echo gate: filtered floor" in caplog.text
    assert "high-pass 150 Hz x3" in caplog.text


async def test_the_echo_gate_reports_the_CONFIGURED_margin_not_the_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AVID-180: the test above passes whether or not the value reaches the service.

    It asserts ``margin 6.0 dB`` — which is also what the parameter *defaulted* to, so it could
    never tell a wired knob from an ignored one. That gap was not theoretical: ``conversation_pi``
    omitted ``barge_in_margin_db`` entirely, so **every echo-gate line ever recorded on the bench
    said 6.0 dB whatever ``/etc/robot/config.toml`` held** — and #106's AC-3, whose settled wording
    is *"the margin that achieves this is measured and recorded"*, was reporting a number the
    operator could not change.

    A deliberately non-default value is the whole point: it is the difference between asserting
    the log line exists and asserting it is *true*."""
    with caplog.at_level(logging.INFO, logger="avid.services.audio"):
        async with _rig(vad_script=[False], barge_in_margin_db=3.5) as rig:
            rig.service._turn_id = uuid4()
            await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
            rig.service._admission(rms_dbfs(b"\x00" * _FRAME_BYTES))
            await rig.service.end_response()

    assert "margin 3.5 dB" in caplog.text
    assert "margin 6.0 dB" not in caplog.text


async def test_a_user_already_talking_when_the_reply_starts_can_still_barge_in() -> (
    None
):
    """AVID-161: barge-in must not depend on catching a *rising* edge.

    When the reply starts while the user is already mid-utterance there is no rising edge left
    to judge them on, so before this the escape hatch was unreachable in exactly the case that
    needed it: the robot talks over you, the uplink shuts, and your words stop reaching the
    model until you give up and start again. The margin is now re-evaluated per frame for the
    duration of the overlap.

    ``barge_in_margin_db=0.0`` makes every frame loud enough — the level arithmetic is proven in
    the domain tests; what is under test here is that the check runs at all once ``_speaking``
    is already True."""
    async with _rig(vad_script=[True], barge_in_margin_db=0.0) as rig:
        await rig.collector.wait_for_type(
            AudioSpeechStarted, 1
        )  # the turn is under way
        assert rig.service._speaking

        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        await rig.collector.wait_for_type(AudioPlaybackFinished, 1)

        assert rig.speaker.stops == 1  # cut off without a fresh rising edge
        finished = rig.collector.of_type(AudioPlaybackFinished)
        assert [e.truncated for e in finished] == [True]  # the model is told (§6.2.4)
        assert not rig.service._uplink_shut()  # and the rest of their turn gets through


async def test_a_quiet_user_talked_over_does_not_interrupt_the_reply() -> None:
    """The other side of the same branch: re-evaluating per frame must not turn every overlap
    into a barge-in, or the robot could never finish a sentence over room noise. With the
    default margin and level-with-the-floor frames, the reply plays on."""
    async with _rig(vad_script=[True]) as rig:
        await rig.collector.wait_for_type(AudioSpeechStarted, 1)

        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        await rig.collector.wait_for_type(AudioPlaybackStarted, 1)
        await rig.collector.settle()

        assert rig.speaker.stops == 0  # the reply was not cut off
        assert rig.collector.of_type(AudioPlaybackFinished) == []


async def test_the_echo_floor_is_measured_on_FILTERED_audio() -> None:
    """AVID-283 AC-1: the filter is not merely constructed, it is *applied* to what the floor sees.

    Driven end to end through the mic loop with the committed empty-room recording — the one the
    issue was filed from — so this fails if the level call site ever stops filtering. It is the
    only test here that would: the `echo gate:` line's wording comes from the configured filter
    object and keeps printing correctly even when nothing uses it, which is exactly the shape of
    "a banner describing something the check does not test".

    ``vad_script=[False]`` keeps every frame in the idle branch, which is the one that seeds
    ``EchoFloor`` — and is also where the real robot spends most of its life."""
    pcm = _read_ambient()
    raw = rms_dbfs(pcm)
    assert raw == pytest.approx(-18.4, abs=1.0)  # the recording is what we think it is

    async with _rig(vad_script=[False], mic_pcm=pcm) as rig:
        # The mic paces frames on real time, so wait on the observable — the floor leaving its
        # silence seed — rather than on a sleep. `EchoFloor` adopts its first frame outright, so
        # one frame through the idle branch is enough to make this meaningful.
        async with asyncio.timeout(_TIMEOUT_S):
            # noqa justification (ASYNC110): the module header's "wait on an Event, never a
            # sleep" rule exists because *publish-then-sleep* is a race. This is the other case
            # — the condition is a service attribute the idle branch updates, and **no event is
            # published for it**, so there is nothing to wait on. Inventing one to satisfy the
            # linter would put test-only machinery on the hot path. Bounded by the timeout above,
            # so a wedged loop still fails fast rather than hanging.
            while rig.service._echo_floor.dbfs <= SILENCE_DBFS:  # noqa: ASYNC110
                await asyncio.sleep(0.005)
        floor = rig.service._echo_floor.dbfs

    # Filtered, the same audio sits tens of dB lower. A floor anywhere near the raw level means
    # the level path is reading broadband again, which is the defect.
    assert floor <= -30.0, (
        f"echo floor {floor:.1f} dBFS is close to the raw {raw:.1f} — the level measurement is "
        f"not being high-passed (AVID-283)"
    )


# --- the proactive origin (#337) -------------------------------------------------------------


async def test_playing_a_proactive_turn_without_adopting_it_is_the_crash_the_rig_found() -> (
    None
):
    """⚠️ The exact failure, at the exact line, reproduced.

    ``_turn_id`` is minted in one place — :meth:`_begin_speech`, the local VAD's rising edge —
    because until M10 every turn began with someone speaking. A proactive turn begins with a clock,
    so this service receives assistant audio for a turn it never heard start, ``_playing_corr`` is
    ``None``, and :meth:`_playback_corr` asserts on the **first chunk**.

    Live on the rig that killed ``ConversationService.pump``: the robot fired its reminder, opened a
    session, and said nothing — while ``proactive_log`` recorded ``delivered``.

    This test pins the raw behaviour deliberately, without the fix, so the assertion below is a
    statement about ``AudioService`` rather than about who remembered to call what.
    """
    async with _rig(vad_script=[False]) as rig:
        chunk = AudioChunk(
            pcm=b"\x00" * _FRAME_BYTES, sample_rate=_SAMPLE_RATE, channels=_CHANNELS
        )
        with pytest.raises(AssertionError):
            await rig.service.play(chunk, item_id="item_0")


async def test_adopting_the_turn_lets_a_proactive_reply_play() -> None:
    """The fix: the origin hands the id over the ``TurnSink`` port before any audio arrives.

    Over the port rather than the bus, because §9.1.4 makes this seam a direct call — audio does not
    belong on an at-most-once bus, and a turn's *identity* travels with its audio.
    """
    async with _rig(vad_script=[False]) as rig:
        corr = uuid4()
        await rig.service.adopt_turn(corr)
        chunk = AudioChunk(
            pcm=b"\x00" * _FRAME_BYTES, sample_rate=_SAMPLE_RATE, channels=_CHANNELS
        )
        await rig.service.play(chunk, item_id="item_0")

        started = [
            e for e in rig.collector.events if isinstance(e, AudioPlaybackStarted)
        ]
        assert started, "playback must open rather than assert"
        assert started[0].correlation_id == corr, (
            "and it must carry the id the trigger minted, or the turn splits in two (§9.1.1)"
        )


# --- The capture watchdog (#347) -------------------------------------------------------------


class _CaptureWatcher:
    """Collects the two ``audio.capture_*`` facts. Registered through ``extra_subs`` because the
    bus freezes its subscriber graph at ``start()`` (§3.5.2)."""

    def __init__(self) -> None:
        self.stalled: list[AudioCaptureStalled] = []
        self.resumed: list[AudioCaptureResumed] = []

    async def handle(self, event: Event) -> None:
        if isinstance(event, AudioCaptureStalled):
            self.stalled.append(event)
        elif isinstance(event, AudioCaptureResumed):
            self.resumed.append(event)


def _watch(watcher: _CaptureWatcher) -> tuple[_ExtraSub, ...]:
    return (
        (AudioCaptureStalled, watcher.handle, "test.capture_stalled"),
        (AudioCaptureResumed, watcher.handle, "test.capture_resumed"),
    )


async def _spin(real_s: float = 0.06) -> None:
    """Let the loop drain, and let the fake mic actually produce a frame.

    ⚠️ A real sleep, unusually for this suite, and the reason is a genuine mismatch:
    ``FakeMicrophone.stream`` paces itself on ``asyncio.sleep`` (real time, like a sample clock)
    while the watchdog sleeps on the injected ``FakeClock`` (virtual). So virtual time must be
    advanced in steps small enough that the mic gets a real chance to stamp in between — which is
    also why the recovery test below walks forward rather than jumping.
    """
    for _ in range(3):
        await asyncio.sleep(0)
    await asyncio.sleep(real_s)
    for _ in range(3):
        await asyncio.sleep(0)


async def test_a_microphone_that_stops_yielding_is_reported() -> None:
    """⚠️ Before this, a mic that stopped delivering produced no observable at all.

    ``AlsaMicrophone`` keeps no counters and skips short reads in a tight loop with no logging, so
    a device that goes away leaves the ``async for`` in ``_run`` parked forever and **nothing in
    the system notices**. A robot that has gone deaf is observationally identical to a room that
    has gone quiet — and §10.5 reads that silence as the user ignoring a proactive turn, three of
    which disable proactivity and blame the user for it.
    """
    watcher = _CaptureWatcher()
    async with _rig(
        vad_script=[False] * 400, capture_stall_s=1.0, extra_subs=_watch(watcher)
    ) as rig:
        await _spin()
        assert rig.mic.chunks_yielded > 0, "precondition: the mic was delivering"

        rig.mic.stall()
        await _spin()
        await rig.clock.advance(2.0)  # past capture_stall_s, twice over
        await _spin()

        assert len(watcher.stalled) == 1, "a mic that stopped delivering said nothing"
        assert watcher.stalled[0].silent_ms >= 1000


async def test_a_stall_is_reported_once_not_every_tick() -> None:
    """Edge-triggered, not a heartbeat.

    A pulse every poll would put steady traffic on a bus whose whole design is that quiet means
    nothing happened, and would drown the one transition a subscriber cares about in repeats of
    itself.
    """
    watcher = _CaptureWatcher()
    async with _rig(
        vad_script=[False] * 400, capture_stall_s=1.0, extra_subs=_watch(watcher)
    ) as rig:
        await _spin()
        rig.mic.stall()
        for _ in range(4):
            await rig.clock.advance(2.0)
            await _spin()

        assert len(watcher.stalled) == 1


async def test_a_recovered_microphone_says_so() -> None:
    """The closing bracket. Without it a subscriber's latch would never clear, and a single
    hiccup would excuse every unanswered reminder for the rest of the process's life."""
    watcher = _CaptureWatcher()
    async with _rig(
        vad_script=[False] * 400, capture_stall_s=1.0, extra_subs=_watch(watcher)
    ) as rig:
        await _spin()
        rig.mic.stall()
        await rig.clock.advance(2.0)
        await _spin()
        assert len(watcher.stalled) == 1

        # Walk forward in steps shorter than the stall window, so the mic can stamp between
        # advances — see _spin.
        rig.mic.resume()
        for _ in range(6):
            await rig.clock.advance(0.2)
            await _spin()

        assert len(watcher.resumed) == 1, "capture came back and nothing said so"


async def test_a_healthy_microphone_is_never_reported_stalled() -> None:
    """The negative control. A watchdog that fires on a working device is worse than none — it
    would excuse every ignored reminder and switch §10.5's backoff off in practice."""
    watcher = _CaptureWatcher()
    async with _rig(
        vad_script=[False] * 400, capture_stall_s=1.0, extra_subs=_watch(watcher)
    ) as rig:
        for _ in range(6):
            await rig.clock.advance(0.4)
            await _spin()

        assert watcher.stalled == []


class _RealisticStopSpeaker(_ParkingSpeaker):
    """A ``_ParkingSpeaker`` whose ``stop()`` behaves like ``AlsaSpeaker.stop()`` (#414).

    ``_ParkingSpeaker.stop`` only *marks* that it was stopped; the test then releases the parked
    write by hand, which puts the release **after** ``interrupt`` has finished. The real adapter
    does both halves itself and in the other order:

    * it sets a flag that ``_write_all`` checks, so the in-flight write **returns immediately** —
      here, releasing the park; and
    * it hops a thread (``await asyncio.to_thread``), so ``stop()`` **yields the loop**.

    Both matter, and together they open a window ``_ParkingSpeaker`` cannot express: the writer can
    resume, run its epoch check and log, all while ``interrupt`` is still suspended inside its own
    first line. That window is #414.
    """

    async def stop(self) -> None:
        await super().stop()
        self.release.set()  # the real flag: the in-flight write returns at once
        await asyncio.sleep(0)  # the real thread hop: interrupt yields here


async def test_a_barge_in_is_not_reported_as_the_device_dropping_audio(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#414: five WARNINGs on the bench that said the speaker dropped audio. It had not.

    ``AudioService._play_chunk`` distinguishes the two causes of a short write by epoch::

        accepted_ms = await self._speaker.play(chunk)
        if self._playback_epoch != epoch:
            _log.debug("barge-in truncated the write: ...")   # expected, benign
            return
        if accepted_ms < submitted_ms:
            _log.warning("speaker accepted %d of %d ms ...")  # the device really dropped it

    and ``interrupt`` bumps that epoch — but only on its **second** line::

        await self._speaker.stop()        # yields, and releases the in-flight write
        episode = self._take_playback()   # the epoch is bumped here

    So a writer released by ``stop()`` can resume, see an unbumped epoch, and take the WARNING
    branch: a barge-in reported as a hardware fault. On the bench this produced **5 WARNINGs and 0
    DEBUG lines** — the branch built to catch it never ran once.

    ⚠️ **What makes it worth fixing is not the noise.** It is that the WARNING is the only signal
    that would show a *genuine* device drop, and it currently cries wolf on ordinary interruption.
    The 432-play differential on #414 found zero real drops precisely because none of those runs
    called ``stop()`` mid-write.
    """
    speaker = _RealisticStopSpeaker()
    with caplog.at_level(logging.DEBUG, logger="avid.services.audio"):
        async with _rig(
            vad_script=[False],
            initial=RobotState.THINKING,
            speaker_factory=lambda _state: speaker,
        ) as rig:
            rig.service._turn_id = uuid4()
            playing = asyncio.create_task(
                rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0"),
                name="test.play",
            )
            try:
                await speaker.entered.wait()  # the write is parked, mid-flight
                await rig.service._begin_speech()  # the barge-in, on the other task
                await playing
            finally:
                speaker.release.set()
                if not playing.done():
                    playing.cancel()

    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "speaker accepted" in r.getMessage()
    ]
    debugs = [
        r.getMessage()
        for r in caplog.records
        if "barge-in truncated the write" in r.getMessage()
    ]
    assert not warnings, (
        "a barge-in was reported as the device dropping audio (#414): "
        f"{warnings}. The epoch must be bumped before interrupt() awaits anything, "
        "or the released writer resumes while the guard still says 'live episode'."
    )
    assert debugs, (
        "the truncated write was neither warned about nor recorded as a barge-in — "
        "the epoch check did not fire and nothing else reported the shortfall"
    )


def _square_pcm(*, amplitude: int, frames: int = _FRAME_BYTES // 2) -> bytes:
    """A square wave at *amplitude* — RMS is exactly the amplitude, so the level is by hand.

    The gate's whole discriminator is loudness, so a test that wants "a person, not the echo"
    needs audio whose dBFS it can state rather than infer.
    """
    return array("h", [amplitude, -amplitude] * (frames // 2)).tobytes()


# ── #467: the robot converses with itself ────────────────────────────────────────────────────


async def test_the_room_still_ringing_after_a_reply_does_not_start_a_turn() -> None:
    """⚠️ **The incident, as a test.** This is the frame that cost $0.50 in eight minutes.

    On the rig 2026-08-24 the robot re-triggered itself **370 ms** after `playback_finished` —
    past the 150 ms echo tail, so `_admits_barge_in` short-circuited to *admit* and nothing was
    compared with anything. It did that 26 times, took 29 turns nobody asked for, and the echo
    gate reported `0 suppressed` throughout, because a gate that refuses nothing and a gate that
    never runs printed identically.

    The guard window keeps judging after the tail has gone, against the floor **frozen** when the
    reply ended — the live floor would have decayed toward ambience by now and a fading echo would
    clear it trivially.
    """
    async with _rig(vad_script=[False], echo_tail_ms=250, guard_window_ms=700) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        await rig.service.end_response()

        await rig.clock.advance(0.37)  # the measured re-trigger, past the tail

        verdict = rig.service._admission(rms_dbfs(b"\x00" * _FRAME_BYTES))
        assert isinstance(verdict, Refused), (
            "a frame 370 ms after the reply was admitted without being judged -- "
            "this is #467 and it spent $0.50 in eight minutes"
        )
        assert verdict.rule == ECHO_TAIL
        assert rig.service.admission_refusals() == {ECHO_TAIL: 1}


async def test_the_owner_answering_promptly_is_still_heard() -> None:
    """The other half, and the one that keeps this a fix rather than a mute button.

    The same instant as the test above — 370 ms after the reply, inside the guard — but the frame
    is well above the frozen floor. A person at conversational distance clears the margin
    trivially (#106 measured real speech at -7.0 dBFS); the robot's own decay cannot, by
    construction. A guard that refused this would be a robot that ignores its owner, which is a
    worse defect than the one being fixed.
    """
    async with _rig(vad_script=[False], echo_tail_ms=250, guard_window_ms=700) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        await rig.service.end_response()
        await rig.clock.advance(0.37)

        loud = rms_dbfs(_square_pcm(amplitude=12000))
        verdict = rig.service._admission(loud)
        assert isinstance(verdict, Admitted), (
            "the owner was refused inside the guard window"
        )
        assert verdict.tested, "a frame inside the guard window was not judged at all"
        assert rig.service.admission_refusals() == {}


async def test_a_suppressed_edge_inside_the_guard_publishes_nothing_and_moves_no_state() -> (
    None
):
    """The refusal has to reach all the way, not merely be counted.

    A gate that logs a refusal but still publishes `audio.speech_started` has changed nothing:
    the state machine still moves to LISTENING, ConversationService still opens a billed session,
    and the turn still happens. That is why this asserts on the bus and the state, not on the
    counter.
    """
    async with _rig(
        vad_script=[False, True, True], echo_tail_ms=250, guard_window_ms=700
    ) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        await rig.service.end_response()
        await rig.clock.advance(0.37)

        before = rig.state.state
        rig.collector.events.clear()
        verdict = rig.service._admission(rms_dbfs(b"\x00" * _FRAME_BYTES))

        assert isinstance(verdict, Refused)
        assert not any(
            isinstance(e, AudioSpeechStarted) for e in rig.collector.events
        ), "a refused edge published audio.speech_started anyway"
        assert rig.state.state is before


async def test_the_guard_expires_so_ordinary_conversation_is_never_judged() -> None:
    """§6.2.4's promise survives the fix: outside the window nothing is tested at all.

    ⚠️ `tested` is the assertion, not the verdict — a quiet frame is *admitted* either way. The
    distinction is the whole point: the gate must be able to say whether it ran.
    """
    async with _rig(vad_script=[False], echo_tail_ms=250, guard_window_ms=700) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        await rig.service.end_response()

        await rig.clock.advance(1.0)  # well past the 700 ms guard

        verdict = rig.service._admission(rms_dbfs(b"\x00" * _FRAME_BYTES))
        assert isinstance(verdict, Admitted)
        assert not verdict.tested, (
            "the guard was still judging after it should have expired"
        )


async def test_a_barge_in_arms_the_guard_even_though_it_arms_no_tail() -> None:
    """⚠️ `interrupt()` used to arm nothing at all, and that was a hole rather than a decision.

    The two windows do different jobs and only one of them belongs to `end_response`:

    * the **streaming tail** is skipped after a barge-in on purpose — `Speaker.stop` closes the
      ALSA handle so there is no drain to wait out, and the user is mid-utterance, so holding the
      uplink shut would clip the very words that interrupted;
    * the **origin guard** must still be armed, because a speaker that was audible a moment ago is
      audible whether it stopped early or late. A self-triggered barge-in is *precisely* the case
      where the guard matters, and it was the one case that skipped it.

    Arming it inside `_take_playback` — the single-taker both finalizers pass through — makes that
    structurally impossible rather than a rule someone has to remember at two call sites.
    """
    async with _rig(vad_script=[False], echo_tail_ms=250, guard_window_ms=700) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        await rig.service.interrupt()

        # No tail: the uplink is open immediately, which is the documented barge-in behaviour.
        rig.service._capture(b"\x33" * _FRAME_BYTES)
        assert rig.service._mic_out.qsize() >= 1

        # ...but the guard IS armed, so a quiet frame still cannot originate a fresh turn.
        await rig.clock.advance(0.1)
        verdict = rig.service._admission(rms_dbfs(b"\x00" * _FRAME_BYTES))
        assert isinstance(verdict, Refused), (
            "interrupt() left the guard unarmed -- a self-triggered barge-in could chain"
        )


async def test_the_echo_tail_is_armed_exactly_once() -> None:
    """⚠️ `end_response` armed it twice until #467, and the second arm was an AVID-174 regression.

    The synchronous arm exists so the deadline can never be applied to a *later* episode a
    concurrent `play()` opened meanwhile — a property that holds only because no `await` separates
    the take from the arm. Re-arming after the publish and the transition put two awaits in the
    middle and reintroduced exactly that race, while the comment above went on claiming otherwise.

    Asserting on the deadline's *value* rather than counting calls is what makes this bite: with
    the second arm restored the clock has advanced across the awaits, so the deadline lands later
    than the one the synchronous arm set.
    """
    async with _rig(vad_script=[False], echo_tail_ms=250) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")

        # ⚠️ The clock must MOVE between the two arms or the test cannot tell them apart, and a
        # first draft of it could not: `FakeClock` advances only when a test says so, so both
        # arms computed the identical deadline and the neutered guard passed. A transition
        # observer is the seam -- `StateManager.watch` is a direct synchronous call inside
        # `transition()`, which is exactly the await the second arm hid behind. Bumping the
        # fake's counter in place is deliberate: `advance()` is a coroutine and this has to
        # happen without yielding, which is the whole property under test.
        def _tick(**_: object) -> None:
            rig.clock._elapsed_ns += 50_000_000  # 50 ms, mid-`end_response`

        rig.state.watch(_tick, name="test.tail_arm_clock")

        armed_from = rig.clock.monotonic_ns()
        await rig.service.end_response()

        expected = armed_from + 250 * 1_000_000
        assert rig.service._uplink_shut_until_ns == expected, (
            "the tail was re-armed after an await -- AVID-174's race, reintroduced"
        )


async def test_the_backstop_bites_on_turns_that_never_proved_they_were_human() -> None:
    """⚠️ A cap on **unproven** origins, not a cap on turns, and the difference is the design.

    The measured runaway ran at 3.25 turns/min; a fast human exchange with this robot is 5-6/min.
    It was *slower than a conversation*, so no rate threshold separates them at any value — a cap
    tight enough to catch it would silence a chatty owner. What separates them is the gap to the
    robot's own reply: 370 ms, against a person who has to hear it end first.

    So the budget counts only back-to-back origins, and once spent the guard stops expiring: every
    origin must clear the frozen floor however long ago playback ended.
    """
    async with _rig(
        vad_script=[False], echo_tail_ms=250, guard_window_ms=700, reactive_budget=2
    ) as rig:
        rig.service._turn_id = uuid4()

        for _ in range(2):
            await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
            await rig.service.end_response()
            await rig.clock.advance(0.3)  # back-to-back: inside reactive_back_to_back_s
            await rig.service._begin_speech()
            rig.service._speaking = False

        # Long past every window, so only the spent budget can still be holding the guard open.
        await rig.clock.advance(30.0)
        verdict = rig.service._admission(rms_dbfs(b"\x00" * _FRAME_BYTES))
        assert isinstance(verdict, Refused), "the backstop never bit"
        assert verdict.rule == REACTIVE_BUDGET

        # ...and it is still not a mute button: a person is heard regardless.
        loud = rig.service._admission(rms_dbfs(_square_pcm(amplitude=12000)))
        assert isinstance(loud, Admitted), "the backstop silenced a real person"


async def test_the_metrics_survive_the_json_the_endpoint_puts_them_through() -> None:
    """#456's lesson, paid once already: a key `json.dumps` cannot render 500s **all** of /metrics.

    `illegal_transitions` uses pre-rendered string keys for exactly this reason — a tuple or enum
    key raises inside the endpoint and takes every other counter down with it. Asserting the round
    trip directly is cheaper than rediscovering it on a Pi at 03:00.
    """
    async with _rig(vad_script=[False]) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        rig.service._admission(rms_dbfs(b"\x00" * _FRAME_BYTES))

        refusals = rig.service.admission_refusals()
        assert json.loads(json.dumps(refusals)) == refusals
        assert all(isinstance(k, str) for k in refusals)
        assert set(refusals) <= ADMISSION_RULES
        assert isinstance(rig.service.reactive_turns(), int)


async def test_admission_refusals_returns_a_copy_the_caller_cannot_corrupt() -> None:
    """A live map handed to `/metrics` is one a reader can mutate under the service."""
    async with _rig(vad_script=[False]) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        rig.service._admission(rms_dbfs(b"\x00" * _FRAME_BYTES))

        snapshot = rig.service.admission_refusals()
        snapshot["echo_floor"] = 99_999
        assert rig.service.admission_refusals() != snapshot


async def test_the_preroll_never_replays_the_robots_own_voice() -> None:
    """⚠️ The amplifier: one marginal admit used to send 300 ms of the robot to the model.

    The ring is fed on every captured frame, echo included, and the replay was unconditional — so
    a single frame scraping past the margin did not send one frame of echo, it sent up to a full
    pre-roll of the robot's contiguous speech, labelled as the user's utterance. That is
    comfortably enough for the server to transcribe and answer, which is how one false admit
    became a conversation.
    """
    async with _rig(vad_script=[False], echo_tail_ms=250, guard_window_ms=700) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")

        # Frames captured while the robot is audible are flagged and must not survive the drain.
        rig.service._preroll.append(b"\x11" * _FRAME_BYTES, echo=True)
        rig.service._preroll.append(b"\x22" * _FRAME_BYTES, echo=True)

        assert rig.service._preroll.drain() == b"", (
            "the robot's own voice was queued for replay as the user's utterance"
        )
