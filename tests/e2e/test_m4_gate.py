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
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from uuid import UUID

import pytest

from avid.adapters import (
    FakeClock,
    FakeMicrophone,
    FakeSpeaker,
    FakeVoiceActivityDetector,
)
from avid.core.event_bus import AsyncioEventBus
from avid.core.hal import AudioChunk, pcm_duration_ms
from avid.core.ports import Speaker
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


_SCRIPT = [False, False, True, True, True, False, False]

# The bench harness, loaded by path (see _load_audio_pi) — not a package, by design.
_DEMO_MODULE = "avid_demo_audio_pi"


async def _drive_one_turn(speaker: Speaker) -> _Collector:
    """Run one scripted turn through the real bus and service; return the collected facts.

    ``_SCRIPT`` = 2 idle-silence pre-roll frames, 3 speech frames, 2 trailing silence
    (= ``silence_hold_ms`` at 10 ms/frame, closing the turn). The pre-roll seeds the
    utterance, so the echoed clip is all 7 captured frames. Shared so the mute-robot gate
    test drives the *identical* loop and differs in one thing only: the speaker.
    """
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    state = StateManager(bus=bus, clock=clock, initial=RobotState.IDLE)
    mic = FakeMicrophone(
        sample_rate=_SAMPLE_RATE,
        channels=_CHANNELS,
        chunk_ms=_CHUNK_MS,
        pcm=_FRAME_PCM,
    )
    vad = FakeVoiceActivityDetector(script=_SCRIPT)
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
        # Stated rather than defaulted (AVID-180). M4 has no assistant playback to echo-gate at
        # all, but a harness that omits a knob reports whatever the default happens to be, and
        # that is how the margin went unmeasured for four bench runs.
        barge_in_margin_db=6.0,
        highpass_hz=150.0,
        highpass_order=3,
        echo_tail_ms=250,
        guard_window_ms=700,
        reactive_window_s=120.0,
        reactive_back_to_back_s=1.5,
        reactive_budget=4,
        capture_stall_s=5.0,
        # The M4 gate proves the transport loopback (no AI client), so echo mode (#103).
        loopback=True,
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
    return collector


async def test_m4_gate_one_turn_loops_back_on_one_correlation_id(
    tmp_path: Path,
) -> None:
    """A scripted turn emits the four audio facts on one id, and the clip is echoed back."""
    script = _SCRIPT
    speaker = FakeSpeaker(out_dir=tmp_path)
    collector = await _drive_one_turn(speaker)

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
    assert p_finished.played_ms == pcm_duration_ms(
        echoed.pcm, sample_rate=_SAMPLE_RATE, channels=_CHANNELS
    )

    # Latency wiring guard: turnaround is non-negative (deterministically 0 under FakeClock).
    assert p_started.monotonic_ns - ended.monotonic_ns >= 0


# --- AVID-91: the gate must be unable to pass on silence -----------------------------------


class _MuteSpeaker(FakeSpeaker):
    """Records every chunk faithfully and reports that the device took none of it.

    The mute robot, reproduced: the PCM is offered, the trace shows it, and nothing plays."""

    async def play(self, chunk: AudioChunk) -> int:
        await super().play(chunk)
        return 0


def _load_audio_pi() -> ModuleType:
    """Import ``docs/demos/audio_pi.py`` by path.

    ``docs/demos`` is not a package and is not on the path — deliberately, since these are
    bench tools rather than shipped code (they build their own object graph outside P3's
    composition root). A file loader is the zero-config way to reach it, and reaching it is
    the point: the *previous* version of this gate's pass/fail logic had no test at all,
    which is how it came to print PASS over a mute robot (AVID-91)."""
    if (cached := sys.modules.get(_DEMO_MODULE)) is not None:
        return cached
    path = Path(__file__).resolve().parents[2] / "docs" / "demos" / "audio_pi.py"
    spec = importlib.util.spec_from_file_location(_DEMO_MODULE, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: @dataclass resolves a slotted class's annotations through
    # sys.modules[cls.__module__], so a module loaded by path but left unregistered blows up
    # inside dataclasses, not in anything this test wrote.
    sys.modules[_DEMO_MODULE] = module
    spec.loader.exec_module(module)
    return module


async def test_a_mute_robot_fails_the_gate() -> None:
    """End to end: a speaker that plays nothing publishes ``played_ms == 0``, and the bench
    harness's own reporter turns that into a **failing** exit code.

    This is the composition the M4 gate was missing. Each half was individually fine — the
    service published a number, the harness checked a latency — and between them a mute robot
    scored a clean PASS. Asserting the two halves *together* is what makes that impossible."""
    speaker = _MuteSpeaker()
    collector = await _drive_one_turn(speaker)

    finished = collector.of_type(AudioPlaybackFinished)[0]
    assert isinstance(finished, AudioPlaybackFinished)
    assert finished.played_ms == 0
    assert speaker.played[0].pcm == _FRAME_PCM * len(
        _SCRIPT
    )  # it was offered the audio

    demo = _load_audio_pi()
    turn = demo._Turn(turnaround_ms=0.1, played_ms=finished.played_ms, elapsed_ms=0.0)
    assert (
        demo._report_loopback([turn], turns=1, budget_ms=200.0, check_playback=True)
        == 1
    )


def test_the_gate_rejects_silence_and_a_wrong_sample_rate() -> None:
    """The reporter's own truth table, in milliseconds, driven by the measured numbers.

    ``(6000, 4010)`` is not invented: it is the real 6.00 s of capture that came back in
    4.01 s when 16 kHz PCM was played through a 24 kHz handle. ``(6000, 0)`` is the mute run.
    Both must fail; a healthy echo and a short cue within the buffer-depth floor must not."""
    demo = _load_audio_pi()

    def verdict(played: int, elapsed: float, *, check: bool = True) -> int:
        turn = demo._Turn(turnaround_ms=1.0, played_ms=played, elapsed_ms=elapsed)
        return int(
            demo._report_loopback(
                [turn], turns=1, budget_ms=200.0, check_playback=check
            )
        )

    assert verdict(6000, 5900.0) == 0  # healthy 6 s echo
    assert verdict(300, 200.0) == 0  # short cue, inside the absolute floor
    assert verdict(0, 0.0) == 1  # defect 1/3: the device took nothing
    assert verdict(6000, 4010.0) == 1  # defect 2: 1.5x fast, the measured numbers
    assert verdict(6000, 0.0) == 1  # mute, but claiming six seconds
    # Behind a fake speaker the divergence half cannot mean anything and is not applied —
    # but a turn that played nothing still fails, on every adapter.
    assert verdict(6000, 0.0, check=False) == 0
    assert verdict(0, 0.0, check=False) == 1


def test_a_disarmed_playback_check_says_so_out_loud(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A check that quietly skips is the defect wearing a hat, so the PASS names its scope."""
    demo = _load_audio_pi()
    turn = demo._Turn(turnaround_ms=1.0, played_ms=880, elapsed_ms=0.0)

    assert (
        demo._report_loopback([turn], turns=1, budget_ms=200.0, check_playback=False)
        == 0
    )
    assert "NOT CHECKED" in capsys.readouterr().out
