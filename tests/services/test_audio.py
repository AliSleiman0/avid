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
    rms_dbfs,
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
    echo_tail_ms: int = 150,
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
        barge_in_margin_db=barge_in_margin_db,
        echo_tail_ms=echo_tail_ms,
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

    Driven from IDLE since AVID-161 gave the LISTENING overlap a legal row: the property under
    test is *"the speaker is live and the machine disagrees"*, so it needs a state the machine
    still refuses to move out of on ``audio.playback_started``. That the set of such states keeps
    shrinking is the point of AVID-161; that the guard does not care is the point of this test."""
    async with _rig(vad_script=[False], initial=RobotState.IDLE) as rig:
        rig.service._turn_id = uuid4()
        # Playback opens from IDLE: the transition is illegal and logged-and-ignored, so the
        # machine never reaches SPEAKING — but the speaker is live either way.
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        assert rig.state.state is RobotState.IDLE

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
    async with _rig(vad_script=[False], echo_tail_ms=150) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")
        await rig.service.end_response()

        rig.service._capture(
            b"\x22" * _FRAME_BYTES
        )  # inside the tail — still the robot
        assert rig.service._mic_out.qsize() == 0

        await rig.clock.advance(0.2)  # past the tail
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

        assert not rig.service._admits_barge_in(rms_dbfs(b"\x00" * _FRAME_BYTES))
        assert rig.service._suppressed_frames == 1


async def test_a_rising_edge_that_clears_the_margin_barges_in() -> None:
    """AC-5 survives the gate: loud enough is the user, and the whole §6.2.4 chain runs."""
    async with _rig(vad_script=[False], barge_in_margin_db=0.0) as rig:
        rig.service._turn_id = uuid4()
        await rig.service.play(_out_chunk(ms=20, fill=1), item_id="item_0")

        assert rig.service._admits_barge_in(rms_dbfs(b"\x00" * _FRAME_BYTES))
        assert rig.service._suppressed_frames == 0


async def test_normal_turn_taking_is_never_tested_against_the_margin() -> None:
    """The gate's blast radius, pinned: the margin only ever judges a rising edge that happens
    while the robot is speaking. With a silent speaker every edge is the user's, whatever the
    margin — so an impossible margin cannot make the robot deaf in ordinary conversation."""
    async with _rig(vad_script=[False], barge_in_margin_db=999.0) as rig:
        assert rig.service._admits_barge_in(rms_dbfs(b"\x00" * _FRAME_BYTES))
        assert rig.service._suppressed_frames == 0


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
            rig.service._admits_barge_in(rms_dbfs(b"\x00" * _FRAME_BYTES))
            await rig.service.end_response()

    assert "echo gate: floor" in caplog.text
    assert "1 suppressed" in caplog.text
    assert "margin 6.0 dB" in caplog.text


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
            rig.service._admits_barge_in(rms_dbfs(b"\x00" * _FRAME_BYTES))
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
