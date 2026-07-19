"""Contract suite for the ``Speaker`` port (AVID-54, SDS §3.9.1, §3.10, §14.4).

A port's contract test runs against *every* adapter, so a fake can never quietly drift from the
real thing (P6). The shared tier is parametrized over the M2.0 hardware seam
(:data:`FAKE_REAL_PARAMS`): the ``"fake"`` case runs everywhere; the ``"real"`` case skips off
the Pi and, on the Pi, exercises the real :class:`AlsaSpeaker` — the same seam
``test_microphone.py`` / ``test_servo.py`` use.

The port promises three things — :meth:`~avid.core.ports.Speaker.play` (stream a chunk),
:meth:`~avid.core.ports.Speaker.play_file` (the degraded-mode canned WAV), and :meth:`stop`
(**barge-in**, "genuinely immediate", SDS §3.10). The shared tests touch only those port
methods, so the fixture is typed as the bare ``Speaker`` port — no local observation Protocol is
needed (unlike the mic/servo suites). The ``FakeSpeaker``-specific tail asserts the off-port
trace (``played``/``files_played``/``stops``) and the "writes a WAV" simulator artifact.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import pytest

from avid.adapters.speaker import FakeSpeaker
from avid.core.hal import AudioChunk
from avid.core.ports import Speaker

from ._hardware import FAKE_REAL_PARAMS, skip_off_pi

# The playback rig matching config/*.toml's [speaker]: 24 kHz S16_LE mono — the Realtime output
# rate (SDS §6.2.4), distinct from the mic's 16 kHz capture.
_SAMPLE_RATE, _CHANNELS = 24000, 1


def _chunk(*, ms: int = 20, fill: int = 0x11) -> AudioChunk:
    """One ``ms``-long S16_LE mono chunk of constant-byte PCM at the playback rate."""
    length = _SAMPLE_RATE * _CHANNELS * 2 * ms // 1000
    return AudioChunk(
        pcm=bytes([fill]) * length, sample_rate=_SAMPLE_RATE, channels=_CHANNELS
    )


def _write_wav(path: Path, *, rate: int, channels: int, ms: int) -> Path:
    """Author an S16_LE WAV of ``ms`` milliseconds of silence — a canned clip for play_file."""
    frames = rate * channels * 2 * ms // 1000
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00" * frames)
    return path


def _only_wav(directory: Path) -> Path:
    """The single ``*.wav`` in *directory* (a sync helper: pathlib globbing off the loop)."""
    wavs = list(directory.glob("*.wav"))
    assert len(wavs) == 1
    return wavs[0]


# --- shared contract: every Speaker adapter must satisfy it ------------------


@pytest.fixture(params=FAKE_REAL_PARAMS)
def speaker(request: pytest.FixtureRequest, tmp_path: Path) -> Speaker:
    """Every Speaker adapter, real and fake, must satisfy the tests below (P6, SDS §14.4).

    The ``"real"`` case skips off the Pi via :func:`skip_off_pi`; on the Pi it constructs
    :class:`AlsaSpeaker` at the same rate/channels the fake gets, so the play/stop contract is
    asserted identically. This replaces the placeholder skip the seam (AVID-50) laid."""
    if request.param == "fake":
        return FakeSpeaker(out_dir=tmp_path)
    skip_off_pi()
    # Imported here, not at module top: pyalsaaudio is Pi-only and absent off the Pi, so only
    # the on-Pi "real" branch ever touches it (P5, ADR-008).
    from avid.adapters.speaker import AlsaSpeaker

    return AlsaSpeaker(device="default", sample_rate=_SAMPLE_RATE, channels=_CHANNELS)


def test_adapter_satisfies_the_speaker_port(speaker: Speaker) -> None:
    assert isinstance(speaker, Speaker)


async def test_play_accepts_a_chunk(speaker: Speaker) -> None:
    """§3.9.1: playing a chunk returns promptly — no blocking write on the loop (P8)."""
    await asyncio.wait_for(speaker.play(_chunk()), timeout=1.0)


async def test_play_file_plays_a_wav(speaker: Speaker, tmp_path: Path) -> None:
    """§3.9.1: the degraded-mode canned WAV plays to completion without stalling the loop."""
    clip = _write_wav(
        tmp_path / "canned.wav", rate=_SAMPLE_RATE, channels=_CHANNELS, ms=60
    )
    await asyncio.wait_for(speaker.play_file(clip), timeout=2.0)


async def test_stop_is_immediate_and_idempotent(speaker: Speaker) -> None:
    """§3.10: barge-in must be instant, and safe to call twice / with nothing playing."""
    await asyncio.wait_for(speaker.stop(), timeout=1.0)
    await asyncio.wait_for(speaker.stop(), timeout=1.0)


async def test_stop_interrupts_playback(speaker: Speaker, tmp_path: Path) -> None:
    """§3.10, the row that bites: a long clip is truncated the moment stop() fires, not after it
    finishes — this is what separates a companion from a kiosk."""
    clip = _write_wav(
        tmp_path / "long.wav", rate=_SAMPLE_RATE, channels=_CHANNELS, ms=1000
    )
    task = asyncio.create_task(speaker.play_file(clip))
    await asyncio.sleep(0.05)
    await speaker.stop()
    # Barge-in cut it short: the ~1 s clip returns well inside its own runtime.
    await asyncio.wait_for(task, timeout=0.5)


# --- FakeSpeaker-specific: the recorded trace + the WAV artifact (SDS §3.9.2) -


async def test_records_played_chunks() -> None:
    fake = FakeSpeaker()
    await fake.play(_chunk())
    await fake.play(_chunk())
    assert len(fake.played) == 2


async def test_records_played_files(tmp_path: Path) -> None:
    clip = _write_wav(
        tmp_path / "canned.wav", rate=_SAMPLE_RATE, channels=_CHANNELS, ms=20
    )
    fake = FakeSpeaker()
    await fake.play_file(clip)
    assert fake.files_played == [clip]


async def test_playing_flag_transitions() -> None:
    fake = FakeSpeaker()
    assert not fake.playing
    await fake.play(_chunk())
    assert fake.playing
    await fake.stop()
    assert not fake.playing


async def test_stop_counts_and_writes_a_wav(tmp_path: Path) -> None:
    """SDS §3.9.2 "FakeSpeaker writes a WAV": stop() flushes the utterance's PCM to out_dir as a
    valid, readable WAV at the chunks' own rate/channels — the eyeball-able artifact."""
    fake = FakeSpeaker(out_dir=tmp_path)
    await fake.play(_chunk(fill=0x11))
    await fake.play(_chunk(fill=0x22))
    await fake.stop()

    assert fake.stops == 1
    with wave.open(str(_only_wav(tmp_path)), "rb") as handle:
        assert handle.getframerate() == _SAMPLE_RATE
        assert handle.getnchannels() == _CHANNELS
        assert handle.getsampwidth() == 2
        assert handle.getnframes() > 0


async def test_play_after_stop_resumes() -> None:
    """A barge-in clears on the next utterance: play() after stop() records normally again."""
    fake = FakeSpeaker()
    await fake.play(_chunk())
    await fake.stop()
    await fake.play(_chunk())
    assert fake.playing
    assert len(fake.played) == 2
