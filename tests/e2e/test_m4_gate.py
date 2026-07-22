"""M4 gate — the permanent CI proof of the audio loopback (AVID-90).

The runnable bench exerciser (``docs/demos/audio_pi.py``) measures the on-Pi latency and VAD
accuracy for the #91 gate; this is its in-process, cross-platform counterpart that re-proves the
loop's shape on every push — the same relationship ``tests/e2e/test_m3_gate.py`` has to
``face_pi.py``.

It drives one scripted speech/silence turn through the **real** ``AsyncioEventBus`` and the real
``AudioService`` with fakes only (no mocks), and asserts the whole M4 loopback invariant: a turn
emits ``speech_started → speech_ended → playback_started → playback_finished``, all carrying a
**single minted ``correlation_id``**, and the captured frames are echoed back to the speaker byte
for byte. Latency itself is a Pi metric (``docs/demos/audio_pi.py``); under ``FakeClock`` the
turnaround is deterministically ``0``, so here it is only a wiring guard (non-negative, right
source).

Drained via an ``asyncio.Event`` collector (``wait_for_type``), never a sleep — so it is stable on
a loaded CI box and on the Windows dev box. Runs clean under ``PYTHONASYNCIODEBUG=1``: the mic
streams an explicit frame (not ``FakeMicrophone``'s tone synth, which would trip the 50 ms
slow-callback gate under coverage).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

from avid.adapters import (
    FakeClock,
    FakeMicrophone,
    FakeSpeaker,
    FakeVoiceActivityDetector,
)
from avid.core.event_bus import AsyncioEventBus
from avid.core.state_manager import StateManager
from avid.domain import (
    AudioPlaybackFinished,
    AudioPlaybackStarted,
    AudioSpeechEnded,
    AudioSpeechStarted,
    Event,
    RobotState,
)
from avid.services import AudioService
from avid.services.audio import _pcm_ms

_SAMPLE_RATE = 16000
_CHANNELS = 1
_CHUNK_MS = 10
_FRAME_BYTES = 320  # 16000 * 1 * 2 * 10 // 1000
_SILENCE_HOLD_MS = 20
_RING_BUFFER_MS = 300
_TIMEOUT_S = 2.0

# A recognizable, non-silent frame: makes "frames carried end to end" a real byte-for-byte
# check, and (being explicit) skips FakeMicrophone's tone synth so the P8 gate stays quiet.
_FRAME_PCM = bytes(i % 256 for i in range(_FRAME_BYTES))

# The four audio facts a turn emits, in causal (publish) order.
_TURN_EVENTS = (
    AudioSpeechStarted,
    AudioSpeechEnded,
    AudioPlaybackStarted,
    AudioPlaybackFinished,
)


class _Collector:
    """Records the audio facts, with an awaitable signal — the ``test_audio.py`` rig shape."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self._arrived = asyncio.Event()

    async def handle(self, event: Event) -> None:
        self.events.append(event)
        self._arrived.set()

    def of_type(self, cls: type[Event]) -> list[Event]:
        return [event for event in self.events if isinstance(event, cls)]

    async def wait_for_type(self, cls: type[Event], count: int) -> None:
        async with asyncio.timeout(_TIMEOUT_S):
            while len(self.of_type(cls)) < count:
                self._arrived.clear()
                if len(self.of_type(cls)) >= count:
                    return
                await self._arrived.wait()


async def test_m4_gate_one_turn_loops_back_on_one_correlation_id(
    tmp_path: Path,
) -> None:
    """A scripted turn emits the four audio facts on one id, and the clip is echoed back.

    ``[False, False, True, True, True, False, False]`` = 2 idle-silence pre-roll frames, 3 speech
    frames, 2 trailing silence (= ``silence_hold_ms`` at 10 ms/frame, closing the turn). The
    pre-roll seeds the utterance, so the echoed clip is all 7 captured frames.
    """
    script = [False, False, True, True, True, False, False]
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    state = StateManager(bus=bus, clock=clock, initial=RobotState.IDLE)
    mic = FakeMicrophone(
        sample_rate=_SAMPLE_RATE,
        channels=_CHANNELS,
        chunk_ms=_CHUNK_MS,
        pcm=_FRAME_PCM,
    )
    speaker = FakeSpeaker(out_dir=tmp_path)
    vad = FakeVoiceActivityDetector(script=script)
    service = AudioService(
        bus=bus,
        clock=clock,
        state=state,
        microphone=mic,
        speaker=speaker,
        vad=vad,
        ring_buffer_ms=_RING_BUFFER_MS,
        sample_rate=_SAMPLE_RATE,
        channels=_CHANNELS,
        silence_hold_ms=_SILENCE_HOLD_MS,
    )

    collector = _Collector()
    # Subscribe before the bus starts (P3); one subscription per fact type.
    for event_type in _TURN_EVENTS:
        bus.subscribe(
            event_type, collector.handle, name=f"m4_gate.{event_type.__name__}"
        )

    async with bus:
        await service.start()
        try:
            await collector.wait_for_type(AudioPlaybackFinished, 1)
        finally:
            await service.stop()

    # Exactly the four facts of one turn — one of each, nothing else. (Cross-subscriber arrival
    # order is not asserted: the bus is FIFO per-subscriber, not across them, #72.)
    for event_type in _TURN_EVENTS:
        assert len(collector.of_type(event_type)) == 1
    started = collector.of_type(AudioSpeechStarted)[0]
    ended = collector.of_type(AudioSpeechEnded)[0]
    p_started = collector.of_type(AudioPlaybackStarted)[0]
    p_finished = collector.of_type(AudioPlaybackFinished)[0]

    # One minted correlation_id propagated across the whole turn.
    turn_id = started.correlation_id
    assert isinstance(turn_id, UUID)
    assert {
        event.correlation_id for event in (started, ended, p_started, p_finished)
    } == {turn_id}
    assert p_started.item_id == p_finished.item_id
    assert p_finished.truncated is False

    # The loopback carries the frames end to end: the echo is the captured clip, byte for byte.
    assert len(speaker.played) == 1
    echoed = speaker.played[0]
    assert echoed.sample_rate == _SAMPLE_RATE
    assert echoed.channels == _CHANNELS
    assert echoed.pcm == _FRAME_PCM * len(script)
    assert p_finished.played_ms == _pcm_ms(
        echoed.pcm, sample_rate=_SAMPLE_RATE, channels=_CHANNELS
    )

    # Latency wiring guard: turnaround is non-negative (deterministically 0 under FakeClock).
    assert p_started.monotonic_ns - ended.monotonic_ns >= 0
