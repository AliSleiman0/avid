"""Speaker adapters — the fake that *is* the simulator, and the real ALSA speaker (AVID-54).

Two implementations of the :class:`~avid.core.ports.Speaker` port, both behind the one contract
suite (P6, SDS §14.4). The port makes three promises — :meth:`play` (stream a chunk),
:meth:`play_file` (the degraded-mode canned-WAV bank), and :meth:`stop` (**barge-in**) — and
both adapters keep them identically (SDS §3.9.1, lines 771-775):

* :class:`FakeSpeaker` records an assertable trace and writes a WAV of what it "played" (SDS
  §3.9.2 "FakeSpeaker writes a WAV"), so the sim can never drift from the real speaker — it is
  the real speaker with no hardware behind it. Stdlib only.
* :class:`AlsaSpeaker` plays to the MAX98357 I2S DAC via ALSA (``pyalsaaudio``). That library is
  pip-on-Pi and absent off it (ADR-008, like ``picamera2``/``adafruit_servokit``/the ReSpeaker
  mic), so it is imported **only** inside this module and **lazily**, inside the worker-thread
  helpers — the module itself imports cleanly on CI and a laptop, where the fake path and mypy
  still need it to load (P5).

Two invariants both adapters share:

* **Blocking playback never touches the event loop** (P8). ALSA writes block, so they run on a
  worker thread via :func:`asyncio.to_thread`; the fake paces itself with :func:`asyncio.sleep`.
* **Barge-in is genuinely immediate** (SDS §3.10, line 851 — "the row that will bite you").
  :meth:`stop` ``set``\\ s a :class:`threading.Event` synchronously (no thread hop to signal it),
  which truncates an in-flight :meth:`play_file` between period writes and, on the real adapter,
  closes the playback handle so its buffered audio is dropped — the interruption the SPEAKING →
  LISTENING transition depends on. The next :meth:`play`/:meth:`play_file` clears the flag, so a
  fresh utterance resumes.

Playback is fixed at **S16_LE, 2 bytes/sample, 24 kHz mono** — the format the Realtime API emits
(SDS §6.2.4, "PCM16 24 kHz mono 16-bit"), distinct from the mic's 16 kHz *capture* rate.
:class:`~avid.core.hal.AudioChunk` carries no bit-depth field, so it is implicit. The fake's
``played``/``files_played``/``stops``/``playing`` are advertised as plain attributes,
deliberately **off** the ``Speaker`` port (no application reads them back, exactly as
``FakeServo.moves`` is off the ``Servo`` port). Constructed only by the composition root or a
test fixture (P3); everything else depends on the port (P2).
"""

from __future__ import annotations

import asyncio
import threading
import wave
from pathlib import Path
from typing import Any

from avid.core.hal import AudioChunk

# S16_LE, 2 bytes per sample per channel — what the Realtime API emits and both adapters play.
_SAMPLE_WIDTH_BYTES = 2

# Playback is sliced into ~20 ms periods (rate // 50): the granularity at which a barge-in
# stop() truncates play_file — one period, not the whole clip — and the ALSA period size.
_PERIOD_DIVISOR = 50
_PERIOD_S = 1 / _PERIOD_DIVISOR


def _slice_wav(path: Path) -> tuple[list[bytes], int, int]:
    """Read an S16_LE WAV and slice it into ~20 ms period chunks (stdlib ``wave``).

    Returns ``(chunks, sample_rate, channels)``. Synchronous — called on a worker thread so the
    file read never touches the loop (P8). The chunks are what :meth:`play_file` writes one at a
    time, checking the interrupt flag between them so a barge-in truncates cleanly."""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        frames = handle.readframes(handle.getnframes())
    period_bytes = max(1, rate // _PERIOD_DIVISOR) * channels * _SAMPLE_WIDTH_BYTES
    chunks = [frames[i : i + period_bytes] for i in range(0, len(frames), period_bytes)]
    return chunks, rate, channels


class FakeSpeaker:
    """The :class:`~avid.core.ports.Speaker` fake (P6): records what it plays, no hardware.

    :meth:`play` records each chunk; :meth:`play_file` streams a canned WAV (paced so a barge-in
    can truncate it); :meth:`stop` marks the barge-in and flushes the utterance's buffered PCM to
    ``out_dir/utterance-{n}.wav`` (SDS §3.9.2 "writes a WAV") — the eyeball-able artifact,
    parallel to ``FakeDisplay``'s PNG sequence. The public :attr:`played`, :attr:`files_played`,
    :attr:`stops` counter and :attr:`playing` flag are the assertable, off-port trace.
    """

    def __init__(self, *, out_dir: Path | None = None) -> None:
        self._out_dir = Path(out_dir) if out_dir is not None else None
        if self._out_dir is not None:
            self._out_dir.mkdir(parents=True, exist_ok=True)
        # Advertised, off the port (the contract's observation points, not an app need).
        self.played: list[AudioChunk] = []
        self.files_played: list[Path] = []
        self.stops = 0
        self.playing = False
        # Barge-in flag + the current utterance's chunks, flushed to a WAV on stop().
        self._interrupted = threading.Event()
        self._buffer: list[AudioChunk] = []
        self._utterance = 0

    async def play(self, chunk: AudioChunk) -> None:
        """Record one played chunk. Clears any prior barge-in — a new chunk is a fresh utterance
        resuming. ``await``\\ s so the call is a real loop yield / cancellation point (P8)."""
        self._interrupted.clear()
        self.playing = True
        self.played.append(chunk)
        self._buffer.append(chunk)
        await asyncio.sleep(0)

    async def play_file(self, path: str | Path) -> None:
        """ "Play" a canned WAV: record its path and pace through it in ~20 ms periods, breaking
        the moment a barge-in :meth:`stop` fires (SDS §3.10). The pacing makes the interruption
        observable — a long clip stops early rather than after the whole file."""
        self._interrupted.clear()
        self.playing = True
        resolved = Path(path)
        self.files_played.append(resolved)
        chunks, _rate, _channels = await asyncio.to_thread(_slice_wav, resolved)
        try:
            for _chunk in chunks:
                if self._interrupted.is_set():
                    break
                await asyncio.sleep(_PERIOD_S)
        finally:
            self.playing = False

    async def stop(self) -> None:
        """Barge-in: stop immediately (SDS §3.10). Sets the interrupt flag (truncating any
        in-flight :meth:`play_file`), ends the utterance, and flushes its buffered PCM to a WAV."""
        self._interrupted.set()
        self.playing = False
        self.stops += 1
        self._flush()

    def _flush(self) -> None:
        """Write the utterance's buffered chunks to ``out_dir/utterance-{n}.wav`` and clear it.

        No-op when nothing was buffered or no ``out_dir`` was injected. The WAV adopts the first
        chunk's rate/channels — the format the chunks actually carried."""
        buffered, self._buffer = self._buffer, []
        if self._out_dir is None or not buffered:
            return
        self._utterance += 1
        first = buffered[0]
        with wave.open(
            str(self._out_dir / f"utterance-{self._utterance}.wav"), "wb"
        ) as handle:
            handle.setnchannels(first.channels)
            handle.setsampwidth(_SAMPLE_WIDTH_BYTES)
            handle.setframerate(first.sample_rate)
            handle.writeframes(b"".join(chunk.pcm for chunk in buffered))


class AlsaSpeaker:
    """The real :class:`~avid.core.ports.Speaker` on the Pi, playing to the MAX98357 via ALSA.

    ``pyalsaaudio`` is imported lazily inside the worker-thread helpers (P5, ADR-008): it is
    Pi-only and absent off the Pi, so keeping it out of module scope lets this module load
    everywhere — the fake path, mypy, and the composition-root import all work off-Pi. Each write
    is blocking, so it runs on a worker thread (P8). :meth:`play` holds one persistent
    ``PCM_PLAYBACK`` handle (opened lazily on first use) so streaming TTS deltas never reopen the
    device; :meth:`stop` sets the interrupt flag and closes that handle on a thread, dropping its
    buffered audio — genuine barge-in (SDS §3.10) — and the next :meth:`play` reopens fresh.
    """

    def __init__(self, *, device: str, sample_rate: int, channels: int) -> None:
        self._device = device
        self._sample_rate = sample_rate
        self._channels = channels
        # The playback handle, built on first use. Untyped (Any) because the library ships no
        # stubs (mypy resolves it via ignore_missing_imports).
        self._pcm: Any | None = None
        self._stopped = threading.Event()

    async def play(self, chunk: AudioChunk) -> None:
        """Write one chunk to the persistent playback handle on a worker thread (P8). Clears any
        prior barge-in first, so a fresh utterance resumes after a :meth:`stop`."""
        self._stopped.clear()
        await asyncio.to_thread(self._write_blocking, chunk.pcm)

    async def play_file(self, path: str | Path) -> None:
        """Play a canned WAV through its own short-lived handle at the file's own format, in
        ~20 ms period writes, breaking the moment a barge-in :meth:`stop` fires (SDS §3.10)."""
        self._stopped.clear()
        chunks, rate, channels = await asyncio.to_thread(_slice_wav, Path(path))
        pcm = await asyncio.to_thread(self._open_blocking, rate, channels)
        try:
            for chunk in chunks:
                if self._stopped.is_set():
                    break
                await asyncio.to_thread(pcm.write, chunk)
        finally:
            await asyncio.to_thread(pcm.close)

    async def stop(self) -> None:
        """Barge-in: set the interrupt flag (immediately, no thread hop) and close the streaming
        handle on a thread so ALSA drops its buffered audio (SDS §3.10)."""
        self._stopped.set()
        await asyncio.to_thread(self._close_blocking)

    def _write_blocking(self, pcm: bytes) -> None:
        # A barge-in landing between the play() call and this thread run skips the stale write.
        if self._stopped.is_set():
            return
        self._ensure_open().write(pcm)

    def _ensure_open(self) -> Any:
        if self._pcm is None:
            self._pcm = self._open_blocking(self._sample_rate, self._channels)
        return self._pcm

    def _open_blocking(self, rate: int, channels: int) -> Any:
        # Lazy, Pi-only import — kept out of module scope so this file loads off-Pi (P5,
        # ADR-008). pyalsaaudio has no stubs; mypy resolves it via ignore_missing_imports.
        import alsaaudio

        return alsaaudio.PCM(
            type=alsaaudio.PCM_PLAYBACK,
            mode=alsaaudio.PCM_NORMAL,
            device=self._device,
            channels=channels,
            rate=rate,
            format=alsaaudio.PCM_FORMAT_S16_LE,
            periodsize=max(1, rate // _PERIOD_DIVISOR),
        )

    def _close_blocking(self) -> None:
        if self._pcm is not None:
            self._pcm.close()
            self._pcm = None
