"""Contract suite for the ``Microphone`` port (AVID-53, SDS §3.9.1, §14.4, §14.8).

A port's contract test runs against *every* adapter, so a fake can never quietly drift from
the real thing (P6). The shared tier is parametrized over the M2.0 hardware seam
(:data:`FAKE_REAL_PARAMS`): the ``"fake"`` case runs everywhere; the ``"real"`` case skips off
the Pi and, on the Pi, exercises the real :class:`AlsaMicrophone` — the same seam
``test_camera.py`` / ``test_servo.py`` use.

The port promises one thing — :meth:`~avid.core.ports.Microphone.stream`, an endless async
iterator of :class:`~avid.core.hal.AudioChunk`. The assertions are made through
:class:`_ConfiguredMicrophone`, a local Protocol that adds the two observation points both
adapters advertise (``sample_rate``, ``channels``) to the port. Those two are deliberately
*off* the port — no application reads them back — exactly as ``FakeServo.moves`` is off the
``Servo`` port.
"""

from __future__ import annotations

import asyncio
import struct
import wave
from pathlib import Path
from typing import Protocol, runtime_checkable

import pytest

from avid.adapters.microphone import FakeMicrophone, _card_name
from avid.core.config import load_config
from avid.core.hal import AudioChunk
from avid.core.ports import Microphone

from ._hardware import FAKE_REAL_PARAMS, skip_off_pi

# One mono rig matching config/*.toml's [microphone]: 16 kHz S16_LE, 20 ms frames.
_SAMPLE_RATE, _CHANNELS, _CHUNK_MS = 16000, 1, 20


@runtime_checkable
class _ConfiguredMicrophone(Microphone, Protocol):
    """The ``Microphone`` port plus the two off-port observation points the contract asserts on.

    Both adapters satisfy it structurally, so the shared fixture is typed against it and
    ``mypy --strict`` stays clean without widening the real port (SDS §3.9.1)."""

    sample_rate: int
    channels: int


async def _take(mic: _ConfiguredMicrophone, n: int) -> list[AudioChunk]:
    """Pull the first *n* chunks off an endless stream, then break out (a bounded reader)."""
    chunks: list[AudioChunk] = []
    async for chunk in mic.stream():
        chunks.append(chunk)
        if len(chunks) >= n:
            break
    return chunks


# --- shared contract: every Microphone adapter must satisfy it ---------------


@pytest.fixture(params=FAKE_REAL_PARAMS)
def microphone(request: pytest.FixtureRequest) -> _ConfiguredMicrophone:
    """Every Microphone adapter, real and fake, must satisfy the tests below (P6, SDS §14.4).

    The ``"real"`` case skips off the Pi via :func:`skip_off_pi`; on the Pi it constructs
    :class:`AlsaMicrophone` with the same rate/channels the fake gets, so the stream contract is
    asserted identically. This replaces the placeholder skip the seam (AVID-50) laid."""
    if request.param == "fake":
        return FakeMicrophone(
            sample_rate=_SAMPLE_RATE, channels=_CHANNELS, chunk_ms=_CHUNK_MS
        )
    skip_off_pi()
    # Imported here, not at module top: pyalsaaudio is Pi-only and absent off the Pi, so only
    # the on-Pi "real" branch ever touches it (P5, ADR-008).
    from avid.adapters.microphone import AlsaMicrophone

    return AlsaMicrophone(
        device="default",
        sample_rate=_SAMPLE_RATE,
        channels=_CHANNELS,
        chunk_ms=_CHUNK_MS,
    )


def test_adapter_satisfies_the_microphone_port(
    microphone: _ConfiguredMicrophone,
) -> None:
    assert isinstance(microphone, Microphone)


async def test_stream_yields_audio_chunks(microphone: _ConfiguredMicrophone) -> None:
    """§3.9.1: the stream yields well-formed chunks — non-empty PCM at the advertised rate."""
    chunks = await _take(microphone, 3)
    assert len(chunks) == 3
    for chunk in chunks:
        assert isinstance(chunk, AudioChunk)
        assert chunk.pcm  # non-empty capture
        assert chunk.sample_rate == microphone.sample_rate
        assert chunk.channels == microphone.channels


async def test_stream_is_cancellable_and_cleans_up(
    microphone: _ConfiguredMicrophone,
) -> None:
    """A preempting shutdown must cancel an in-flight stream — it stops rather than running on."""

    async def _drain() -> None:
        async for _ in microphone.stream():
            pass

    task = asyncio.create_task(_drain())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_bounded_read_does_not_stall_the_loop(
    microphone: _ConfiguredMicrophone,
) -> None:
    """P8: a bounded read returns promptly — no blocking capture on the loop."""
    chunks = await asyncio.wait_for(_take(microphone, 3), timeout=1.0)
    assert len(chunks) == 3


# --- FakeMicrophone-specific: the synthesized/streamed buffer (SDS §14.8) ----


async def test_chunk_length_matches_chunk_ms() -> None:
    """Each chunk is exactly one ``chunk_ms`` slice of S16_LE audio (rate·channels·2·ms/1000)."""
    fake = FakeMicrophone(
        sample_rate=_SAMPLE_RATE, channels=_CHANNELS, chunk_ms=_CHUNK_MS
    )
    chunks = await _take(fake, 1)
    assert len(chunks[0].pcm) == _SAMPLE_RATE * _CHANNELS * 2 * _CHUNK_MS // 1000  # 640


async def test_stream_cycles_endlessly() -> None:
    """A two-chunk buffer repeats: chunk 0 and chunk 2 are identical, chunk 1 differs — proof
    the stream wraps rather than running dry."""
    one_chunk = _SAMPLE_RATE * _CHANNELS * 2 * _CHUNK_MS // 1000
    pcm = b"\x01" * one_chunk + b"\x02" * one_chunk
    fake = FakeMicrophone(
        sample_rate=_SAMPLE_RATE, channels=_CHANNELS, chunk_ms=_CHUNK_MS, pcm=pcm
    )
    chunks = await _take(fake, 3)
    assert chunks[0].pcm == b"\x01" * one_chunk
    assert chunks[1].pcm == b"\x02" * one_chunk
    assert chunks[2].pcm == chunks[0].pcm


async def test_chunks_yielded_counts_up() -> None:
    fake = FakeMicrophone(
        sample_rate=_SAMPLE_RATE, channels=_CHANNELS, chunk_ms=_CHUNK_MS
    )
    assert fake.chunks_yielded == 0
    await _take(fake, 2)
    assert fake.chunks_yielded == 2


async def test_stream_marks_closed_on_cleanup() -> None:
    """Closing the iterator runs the generator's ``finally`` — the observable cleanup a bounded
    reader / cancel triggers."""
    fake = FakeMicrophone(
        sample_rate=_SAMPLE_RATE, channels=_CHANNELS, chunk_ms=_CHUNK_MS
    )
    agen = fake.stream()
    await agen.__anext__()
    assert not fake.closed
    await agen.aclose()
    assert fake.closed


async def test_from_wav_streams_the_file(tmp_path: Path) -> None:
    """`from_wav` adopts the WAV's own rate/channels (SDS §3.9.2 "streams a WAV")."""
    path = tmp_path / "clip.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(struct.pack("<h", 1234) * 2 * 200)

    fake = FakeMicrophone.from_wav(path, chunk_ms=10)
    assert (fake.sample_rate, fake.channels) == (8000, 2)
    chunks = await _take(fake, 1)
    assert chunks[0].sample_rate == 8000
    assert chunks[0].channels == 2
    assert chunks[0].pcm


def test_from_wav_rejects_non_16bit(tmp_path: Path) -> None:
    """Format is fixed at S16_LE; an 8-bit WAV is refused loudly rather than mis-streamed."""
    path = tmp_path / "eight_bit.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(1)
        handle.setframerate(8000)
        handle.writeframes(b"\x80" * 100)

    with pytest.raises(ValueError, match="16-bit"):
        FakeMicrophone.from_wav(path)


_PI_TOML = Path(__file__).resolve().parents[2] / "config" / "pi.toml"


# --- the capture-mixer check (AVID-296) -------------------------------------


@pytest.mark.parametrize(
    ("device", "expected"),
    [
        ("plughw:CARD=Device,DEV=0", "Device"),  # the shipped pi.toml value
        ("plughw:CARD=seeed2micvoicec,DEV=0", "seeed2micvoicec"),
        ("hw:CARD=Device", "Device"),
        ("default", None),  # no card named — the check skips rather than guessing
        ("hw:1,0", None),  # index form, no CARD= to read
        ("plughw:CARD=,DEV=0", None),  # empty is not a card name
    ],
)
def test_card_name_is_parsed_from_the_device_string(
    device: str, expected: str | None
) -> None:
    """The one pure piece of AVID-296's check, so it is testable off-Pi.

    Everything around it needs ALSA and cannot run here; this is the part that decides *which*
    card gets inspected, and getting it wrong would silently skip the check on the very rig it
    was written for."""
    assert _card_name(device) == expected


def test_the_shipped_pi_device_names_a_card() -> None:
    """A guard against the check quietly becoming a no-op.

    If ``[microphone] device`` were ever changed to ``default`` or an index form, ``_card_name``
    returns ``None`` and the AGC inspection skips — with a DEBUG line nobody reads. That is
    precisely the silent-skip failure AVID-296 is about, one level up, so the shipped value is
    asserted rather than assumed."""
    config = load_config(_PI_TOML)

    assert _card_name(config.microphone.device) == "Device"
