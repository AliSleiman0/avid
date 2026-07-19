"""Microphone adapters — the fake that *is* the simulator, and the real ALSA mic (AVID-53).

Two implementations of the :class:`~avid.core.ports.Microphone` port, both behind the one
contract suite (P6, SDS §14.4). The port makes a single promise — :meth:`stream`, an endless
async iterator of :class:`~avid.core.hal.AudioChunk` — and both adapters keep it identically
(SDS §3.9.1):

* :class:`FakeMicrophone` streams synthesized PCM (or a WAV, via :meth:`from_wav`) and *is*
  the simulator, so the sim can never drift from the real mic — it is the real mic with no
  hardware behind it (SDS §3.9.2). Stdlib only.
* :class:`AlsaMicrophone` captures from the ReSpeaker via ALSA (``pyalsaaudio``). That library
  is pip-on-Pi and absent off it (ADR-008, like ``picamera2``/``adafruit_servokit``), so it is
  imported **only** inside this module and **lazily**, inside the worker-thread helper — the
  module itself imports cleanly on CI and a laptop, where the fake path and mypy still need it
  to load (P5).

Two invariants both adapters share:

* **Blocking capture never touches the event loop** (P8). ALSA reads block, so they run on a
  worker thread via :func:`asyncio.to_thread`; the fake paces itself with :func:`asyncio.sleep`.
* **The stream is endless and cancellable.** A real mic streams until stopped, so both are
  infinite async generators; a bounded reader ``break``\\ s out or the task is cancelled, and
  the generator's ``finally`` cleans up (closes the PCM handle; marks the fake closed).

``sample_rate``/``channels`` are advertised as plain attributes, deliberately **off** the
``Microphone`` port: no application reads them back, so they are not an application need — they
are the contract suite's observation points (exactly as ``FakeCamera.captures`` is off the
``Camera`` port). Audio is fixed at **S16_LE, 2 bytes/sample** — the ALSA/ReSpeaker standard;
:class:`~avid.core.hal.AudioChunk` carries no bit-depth field, so it is implicit. Constructed
only by the composition root or a test fixture (P3); everything else depends on the port (P2).
"""

from __future__ import annotations

import asyncio
import math
import struct
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from avid.core.hal import AudioChunk

# The one PCM sample format the adapters exchange: signed 16-bit little-endian, 2 bytes per
# sample per channel — what the ReSpeaker captures and what the Realtime API / VAD expect.
_SAMPLE_WIDTH_BYTES = 2


def _chunk_bytes(sample_rate: int, channels: int, chunk_ms: int) -> int:
    """Byte length of one ``chunk_ms`` slice of S16_LE audio (at least one byte)."""
    return max(1, sample_rate * channels * _SAMPLE_WIDTH_BYTES * chunk_ms // 1000)


def _synth_tone(
    sample_rate: int, channels: int, *, freq_hz: float = 220.0, duration_s: float = 1.0
) -> bytes:
    """Synthesize ``duration_s`` of an S16_LE sine tone — the fake's default source.

    A pure tone, not silence, so a consumer sees non-trivial samples; interleaved across
    ``channels``. Stdlib only (``math``/``struct``): no numpy, matching ``FakeCamera``'s
    "a synthetic frame is a bytes fill" ethos (SDS §14.8)."""
    amplitude = 8000
    frames = bytearray()
    for i in range(int(sample_rate * duration_s)):
        sample = int(amplitude * math.sin(2 * math.pi * freq_hz * i / sample_rate))
        frames += struct.pack("<h", sample) * channels
    return bytes(frames)


class FakeMicrophone:
    """The :class:`~avid.core.ports.Microphone` fake (P6): streams PCM, no hardware.

    ``sample_rate``/``channels``/``chunk_ms`` are injected (P7). With no ``pcm`` a default tone
    is synthesized; :meth:`from_wav` feeds a real recording instead (SDS §3.9.2 "streams a
    WAV"). :meth:`stream` yields ``chunk_ms`` slices endlessly, cycling the buffer and sleeping
    between yields so it never blocks the loop (P8) and a cancel/``break`` stops it cleanly. The
    public :attr:`chunks_yielded` counter and :attr:`closed` flag are assertable in tests.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int,
        chunk_ms: int,
        pcm: bytes | None = None,
    ) -> None:
        # Advertised, off the port (the contract's observation points, not an app need).
        self.sample_rate = sample_rate
        self.channels = channels
        self._chunk_ms = chunk_ms
        self._chunk_bytes = _chunk_bytes(sample_rate, channels, chunk_ms)
        buf = pcm if pcm else _synth_tone(sample_rate, channels)
        # Grow to at least one chunk, then trim to a whole number of chunks, so every cycled
        # slice is a full chunk and the wrap is seamless.
        if len(buf) < self._chunk_bytes:
            buf *= -(-self._chunk_bytes // len(buf))  # ceil-division repeat
        self._buf = buf[: (len(buf) // self._chunk_bytes) * self._chunk_bytes]
        # Public, assertable: how many chunks were handed out, and whether the stream cleaned up.
        self.chunks_yielded = 0
        self.closed = False

    @classmethod
    def from_wav(cls, path: str | Path, *, chunk_ms: int = 20) -> FakeMicrophone:
        """Build a fake that streams the S16_LE WAV at *path* (SDS §3.9.2, §14.8).

        Read synchronously via stdlib ``wave`` at construction time — before the event loop
        runs, like config load — so P8 is not implicated. The WAV's own rate/channels are
        adopted, so the streamed chunks advertise the file's format."""
        with wave.open(str(path), "rb") as handle:
            if handle.getsampwidth() != _SAMPLE_WIDTH_BYTES:
                raise ValueError(
                    f"from_wav expects 16-bit PCM (S16_LE); {path} is "
                    f"{handle.getsampwidth() * 8}-bit"
                )
            return cls(
                sample_rate=handle.getframerate(),
                channels=handle.getnchannels(),
                chunk_ms=chunk_ms,
                pcm=handle.readframes(handle.getnframes()),
            )

    async def stream(self) -> AsyncIterator[AudioChunk]:
        """Yield ``chunk_ms`` slices of the buffer, endlessly and cancellably (P8).

        Sleeps one ``chunk_ms`` between yields (a real mic paces at the sample clock), so the
        loop stays clear and a cancel raised mid-sleep propagates. The ``finally`` marks the
        stream closed — the observable cleanup a bounded reader / cancel triggers."""
        step_s = self._chunk_ms / 1000
        offset = 0
        try:
            while True:
                await asyncio.sleep(step_s)
                chunk = self._buf[offset : offset + self._chunk_bytes]
                offset = (offset + self._chunk_bytes) % len(self._buf)
                self.chunks_yielded += 1
                yield AudioChunk(
                    pcm=chunk, sample_rate=self.sample_rate, channels=self.channels
                )
        finally:
            self.closed = True


class AlsaMicrophone:
    """The real :class:`~avid.core.ports.Microphone` on the Pi, capturing via ALSA.

    ``pyalsaaudio`` is imported lazily inside :meth:`_open_blocking` (P5, ADR-008): it is
    Pi-only and absent off the Pi, so keeping it out of module scope lets this module load
    everywhere — the fake path, mypy, and the composition-root import all work off-Pi. Opening
    the device and each ``read`` are blocking, so they run on a worker thread (P8); the awaited
    hop between reads is where a preempting cancel takes effect. ``sample_rate``/``channels``
    are advertised off the port, mirroring :class:`FakeMicrophone`.
    """

    def __init__(
        self, *, device: str, sample_rate: int, channels: int, chunk_ms: int
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self._device = device
        self._periodsize = max(1, sample_rate * chunk_ms // 1000)

    async def stream(self) -> AsyncIterator[AudioChunk]:
        """Open the capture device on a worker thread, then yield captured chunks (P8).

        Each blocking ``read`` hops to a thread; a short read (``length <= 0``) is skipped
        rather than yielded. The ``finally`` closes the handle on a thread when the reader
        stops (``break``/cancel), so the device is always released."""
        pcm = await asyncio.to_thread(self._open_blocking)
        try:
            while True:
                length, data = await asyncio.to_thread(pcm.read)
                if length <= 0 or not data:
                    continue
                yield AudioChunk(
                    pcm=bytes(data),
                    sample_rate=self.sample_rate,
                    channels=self.channels,
                )
        finally:
            await asyncio.to_thread(pcm.close)

    def _open_blocking(self) -> Any:
        # Lazy, Pi-only import — kept out of module scope so this file loads off-Pi (P5,
        # ADR-008). pyalsaaudio has no stubs; mypy resolves it via ignore_missing_imports.
        import alsaaudio

        return alsaaudio.PCM(
            type=alsaaudio.PCM_CAPTURE,
            mode=alsaaudio.PCM_NORMAL,
            device=self._device,
            channels=self.channels,
            rate=self.sample_rate,
            format=alsaaudio.PCM_FORMAT_S16_LE,
            periodsize=self._periodsize,
        )
