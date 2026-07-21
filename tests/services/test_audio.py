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
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import NamedTuple

from avid.adapters.clock import FakeClock
from avid.adapters.microphone import FakeMicrophone
from avid.adapters.speaker import FakeSpeaker
from avid.adapters.vad import FakeVoiceActivityDetector
from avid.core.event_bus import AsyncioEventBus
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
from avid.services.audio import AudioService, _pcm_ms

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
    assert _pcm_ms(b"\x00" * 48, sample_rate=24000, channels=1) == 1
    assert _pcm_ms(b"\x00" * 96, sample_rate=24000, channels=1) == 2
    assert (
        _pcm_ms(b"\x00" * 32, sample_rate=16000, channels=1) == 1
    )  # the loopback rate
    assert _pcm_ms(b"", sample_rate=24000, channels=1) == 0


# --- the service shape (SDS §9.2) ----------------------------------------------------------


async def test_subscriptions_are_empty_at_m4() -> None:
    """AC-4 seam: the conversation.* Event types AudioService will subscribe to do not
    exist until M5, so there is nothing to declare — the loopback stands in for now."""
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


# --- AC-1 / AC-2 / AC-3: a full turn -------------------------------------------------------


async def test_a_full_turn_publishes_the_four_audio_facts_on_one_correlation_id() -> (
    None
):
    """The happy path end to end: two silent frames of pre-roll, three of speech, two of
    trailing silence to close it. Exactly one of each fact, all on the minted turn id.

    ring_buffer_ms = the drained pre-roll: frames 0,1 (silence) plus frame 2 (the first
    speech frame, appended before the drain) = 3 frames x 10 ms = 30 ms. duration_ms = the
    three *speech* frames = 30 ms (the trailing silence is not speech). played_ms = the
    whole captured clip, frames 0..6 = 7 x 320 B = 2240 B over 32 B/ms = 70 ms.
    """
    script = [False, False, True, True, True, False, False]
    async with _rig(vad_script=script) as rig:
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
    async with _rig(vad_script=script) as rig:
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
