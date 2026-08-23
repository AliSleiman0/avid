"""Speaker adapters — the fake that *is* the simulator, and the real ALSA speaker (AVID-54).

Two implementations of the :class:`~avid.core.ports.Speaker` port, both behind the one contract
suite (P6, SDS §14.4). The port makes three promises — :meth:`play` (stream a chunk),
:meth:`play_file` (the degraded-mode canned-WAV bank), and :meth:`stop` (**barge-in**) — and
both adapters keep them identically (SDS §3.9.1, lines 771-775):

* :class:`FakeSpeaker` records an assertable trace and writes a WAV of what it "played" (SDS
  §3.9.2 "FakeSpeaker writes a WAV"), so the sim can never drift from the real speaker — it is
  the real speaker with no hardware behind it. Stdlib only.
* :class:`AlsaSpeaker` plays to the MAX98357 I2S DAC via ALSA (``pyalsaaudio``). That library is
  pip-on-Pi and absent off it (ADR-008, like ``picamera2``/``adafruit_servokit``), so it is
  imported **only** inside this module and **lazily** — inside the worker-thread
  helpers, plus one guarded resolution at construction time (:func:`_alsa_error_type`, which is
  reached only on the Pi-only ``alsa`` branch of the composition root and falls back cleanly
  everywhere else). The module itself imports cleanly on CI and a laptop, where the fake path
  and mypy still need it to load (P5).

Three invariants both adapters share:

* **Blocking playback never touches the event loop** (P8). ALSA writes block, so they run on a
  worker thread via :func:`asyncio.to_thread`; the fake paces itself with :func:`asyncio.sleep`.
* **Barge-in is genuinely immediate** (SDS §3.10, line 851 — "the row that will bite you").
  :meth:`stop` ``set``\\ s a :class:`threading.Event` synchronously (no thread hop to signal it),
  which truncates in-flight playback between ~20 ms period writes and, on the real adapter,
  closes the playback handle so its buffered audio is dropped — the interruption the SPEAKING →
  LISTENING transition depends on. The next :meth:`play`/:meth:`play_file` clears the flag, so a
  fresh utterance resumes.
* **Playback reports what the device took, never what it was handed** (AVID-91). Both methods
  return the milliseconds *accepted*. Discarding ALSA's return is how the M4 gate came to print
  ``PASS`` while the robot was mute: a persistent handle used intermittently underruns, and the
  next ``write()`` returns ``-EPIPE`` instantly having played nothing (measured on the Pi:
  ``write 1: 48000 @1.898s / write 2: -32 @0.000s / write 3: 48000 @1.909s``). *Accepted* is
  honest but not perfect — a device acknowledges frames into its ring buffer, not out of its
  DAC — so it is exact about drops and optimistic by up to one buffer depth (~107 ms at 24 kHz)
  about audible sound. See :meth:`~avid.core.ports.Speaker.play`.

Sample **width** is fixed at S16_LE, 2 bytes/sample — :class:`~avid.core.hal.AudioChunk` carries
no bit-depth field, so it is implicit. Sample **rate** and **channel count** are *not* implicit:
every chunk carries them and both adapters honour them. :class:`AlsaSpeaker` tracks the format
its handle was opened at and reopens the device when a chunk declares another (``device
"default"`` goes through ALSA's ``plug``/``dmix``, so 16 kHz opens fine); :class:`FakeSpeaker`
records them and writes its WAV at the chunks' own format. ``[speaker] sample_rate``/``channels``
are the **nominal** format the rig is tuned around — 24 kHz mono, the rate the Realtime API emits
(SDS §6.2.4), distinct from the mic's 16 kHz capture — and a chunk that deviates is logged once,
never silently obeyed. Playing 16 kHz PCM at a configured 24 kHz is 1.5x fast and a fifth high,
which is subtle enough in a room to survive three weeks of bench runs (AVID-91).

The fake's ``played``/``files_played``/``stops``/``playing`` are advertised as plain attributes,
deliberately **off** the ``Speaker`` port (no application reads them back, exactly as
``FakeServo.moves`` is off the ``Servo`` port). Constructed only by the composition root or a
test fixture (P3); everything else depends on the port (P2).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import wave
from pathlib import Path
from typing import Any

from avid.core.hal import (
    SAMPLE_WIDTH_BYTES,
    AudioChunk,
    frames_duration_ms,
    pcm_duration_ms,
)

_log = logging.getLogger(__name__)

# Playback is sliced into ~20 ms periods (rate // 50): the granularity at which a barge-in
# stop() truncates playback — one period, not the whole clip — and the ALSA period size.
_PERIOD_DIVISOR = 50
_PERIOD_S = 1 / _PERIOD_DIVISOR


class _NoAlsaError(Exception):
    """Stand-in for ``alsaaudio.ALSAAudioError`` where the library is absent.

    Unreachable by construction: off the Pi there is no ALSA handle to raise anything, so an
    ``except`` clause naming this class simply never fires. It exists so :class:`AlsaSpeaker`
    can name its recovery exception as an ordinary attribute — which is also the seam the
    adapter unit tests substitute their own error class through, with no monkeypatching.
    """


def _alsa_error_type() -> Any:
    """The library's error class, or :class:`_NoAlsaError` where it is absent.

    Annotated ``Any`` deliberately: ``pyalsaaudio`` ships no stubs (mypy resolves it via
    ``ignore_missing_imports``), so declaring ``type[BaseException]`` would return ``Any``
    from an untyped module and trip ``warn_return_any``. ``Any`` keeps the ``except`` clause
    legal with no ``# type: ignore``.
    """
    try:
        import alsaaudio
    except ImportError:  # pragma: no cover - off-Pi only; the Pi always has the library
        return _NoAlsaError
    return alsaaudio.ALSAAudioError


def _read_wav(path: Path) -> tuple[bytes, int, int]:
    """Read an S16_LE WAV whole (stdlib ``wave``); return ``(frames, sample_rate, channels)``.

    Synchronous — called on a worker thread so the file read never touches the loop (P8).
    Returns the PCM undivided: the caller slices it into periods, because the two adapters
    slice for different reasons (the fake to pace itself, the real one to bound barge-in
    latency and to keep a dropped period from costing the whole utterance)."""
    with wave.open(str(path), "rb") as handle:
        return (
            handle.readframes(handle.getnframes()),
            handle.getframerate(),
            handle.getnchannels(),
        )


def _period_bytes(rate: int, channels: int) -> int:
    """Bytes in one ~20 ms period of S16_LE audio at this format (never zero)."""
    return max(1, rate // _PERIOD_DIVISOR) * channels * SAMPLE_WIDTH_BYTES


class FakeSpeaker:
    """The :class:`~avid.core.ports.Speaker` fake (P6): records what it plays, no hardware.

    :meth:`play` records each chunk; :meth:`play_file` streams a canned WAV (paced so a barge-in
    can truncate it); :meth:`stop` marks the barge-in and flushes the utterance's buffered PCM to
    ``out_dir/utterance-{n}.wav`` (SDS §3.9.2 "writes a WAV") — the eyeball-able artifact,
    parallel to ``FakeDisplay``'s PNG sequence. The public :attr:`played`, :attr:`files_played`,
    :attr:`stops` counter and :attr:`playing` flag are the assertable, off-port trace.

    Having no device, it accepts everything it is given: :meth:`play` returns the chunk's whole
    duration. That is not a shortcut — a fake that always reported success would make the port's
    return meaningless, so the honest fake reports what a *working* device does, and a test that
    needs a lossy speaker subclasses this one (see ``tests/services/test_audio.py``).
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

    async def play(self, chunk: AudioChunk) -> int:
        """Record one played chunk; return its whole duration in ms. Clears any prior barge-in —
        a new chunk is a fresh utterance resuming. ``await``\\ s so the call is a real loop yield
        / cancellation point (P8)."""
        self._interrupted.clear()
        self.playing = True
        self.played.append(chunk)
        self._buffer.append(chunk)
        await asyncio.sleep(0)
        return pcm_duration_ms(
            chunk.pcm, sample_rate=chunk.sample_rate, channels=chunk.channels
        )

    async def play_file(self, path: str | Path) -> int:
        """ "Play" a canned WAV: record its path and pace through it in ~20 ms periods, breaking
        the moment a barge-in :meth:`stop` fires (SDS §3.10). The pacing makes the interruption
        observable — a long clip stops early rather than after the whole file — and the returned
        ms is what was paced *before* the break, so a truncated cue reports its real length."""
        self._interrupted.clear()
        self.playing = True
        resolved = Path(path)
        self.files_played.append(resolved)
        pcm, rate, channels = await asyncio.to_thread(_read_wav, resolved)
        period = _period_bytes(rate, channels)
        frame_bytes = max(1, channels * SAMPLE_WIDTH_BYTES)
        played_frames = 0
        try:
            for start in range(0, len(pcm), period):
                if self._interrupted.is_set():
                    break
                await asyncio.sleep(_PERIOD_S)
                played_frames += len(pcm[start : start + period]) // frame_bytes
        finally:
            self.playing = False
        return frames_duration_ms(played_frames, sample_rate=rate)

    async def stop(self) -> None:
        """Barge-in: stop immediately (SDS §3.10). Sets the interrupt flag (truncating any
        in-flight :meth:`play_file`), ends the utterance, and flushes its buffered PCM to a WAV."""
        self._interrupted.set()
        self.playing = False
        self.stops += 1
        self._flush()
        # A real loop yield, like play()'s — and for the same P6 reason. AlsaSpeaker.stop hops a
        # thread (to_thread), so on hardware a barge-in genuinely suspends and other tasks run
        # inside it. Without this the fake made barge-in *atomic* with respect to the pump, so
        # AVID-174's interleaving was not expressible in CI at all and the crash reached the Pi.
        await asyncio.sleep(0)

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
            handle.setsampwidth(SAMPLE_WIDTH_BYTES)
            handle.setframerate(first.sample_rate)
            handle.writeframes(b"".join(chunk.pcm for chunk in buffered))


class AlsaSpeaker:
    """The real :class:`~avid.core.ports.Speaker` on the Pi, playing to the MAX98357 via ALSA.

    ``pyalsaaudio`` is imported lazily inside the worker-thread helpers (P5, ADR-008): it is
    Pi-only and absent off the Pi, so keeping it out of module scope lets this module load
    everywhere — the fake path, mypy, and the composition-root import all work off-Pi. Each write
    is blocking, so it runs on a worker thread (P8).

    **One handle, reopened on demand.** :meth:`play` and :meth:`play_file` share a single
    ``PCM_PLAYBACK`` handle, opened lazily and reopened whenever the audio's format changes, so
    streaming TTS deltas at one rate never reopen the device while a 16 kHz echo or a canned cue
    at another rate still plays correctly. (The previous design gave ``play_file`` a second,
    private handle on the same device — tolerated by ``plug``/``dmix``, ``EBUSY`` against a raw
    ``hw:`` device.) :meth:`stop` sets the interrupt flag and closes the handle on a thread,
    dropping its buffered audio — genuine barge-in (SDS §3.10) — and the next play reopens fresh.

    **Underrun recovery is the whole point (AVID-91).** A persistent handle written to
    intermittently underruns between utterances; ALSA then fails the *next* write instantly,
    having played nothing. Every period write therefore checks its result and, on any of the
    four failure shapes the library can produce (negative return, zero, a short count, or a
    raised ``ALSAAudioError``), closes and reopens the handle and retries that period **once** —
    the recovery the Pi measurement supports, since a fresh handle never drops. A period that
    fails twice is counted as lost and logged; it never raises, because a dropped 20 ms of audio
    must not kill a turn.

    **Concurrency.** One :class:`threading.Lock` guards the handle and the format it was opened
    at. It is held across a whole write loop, not per period: releasing between periods would let
    a barge-in close the handle mid-utterance and the loop then reopen and resume the audio the
    user just interrupted. Barge-in stays immediate anyway because :meth:`stop` sets its
    :class:`threading.Event` *before* taking the lock, so the loop exits at the next period
    boundary — a wait bounded by one blocking period write (~21 ms at 24 kHz), against SDS
    §3.10's requirement and the contract suite's 500 ms assertion.

    **Invariant the caller must keep:** one :meth:`play` in flight at a time. A write occupies
    one ``to_thread`` worker and a concurrent :meth:`stop` needs a second; ``AudioService``
    awaits each play before issuing the next, so at most one is ever outstanding. A future caller
    that fires deltas without awaiting would turn thread-pool exhaustion into a hang rather than
    a crash — which would be miserable to diagnose, hence this paragraph.
    """

    def __init__(self, *, device: str, sample_rate: int, channels: int) -> None:
        self._device = device
        # The NOMINAL format (§9.6 [speaker]) — what the rig is tuned around, not a rate
        # imposed on the audio. Every chunk carries its own format and is honoured; a
        # deviation from these is announced once (see _note_format).
        self._nominal_rate = sample_rate
        self._nominal_channels = channels
        self._nominal_warned = False
        # The playback handle and the format it was opened at, built on first use. Untyped
        # (Any) because the library ships no stubs (mypy resolves it via ignore_missing_imports).
        self._pcm: Any | None = None
        self._open_rate: int | None = None
        self._open_channels: int | None = None
        self._stopped = threading.Event()
        # Guards _pcm/_open_rate/_open_channels across a whole write loop (see the class docs).
        self._lock = threading.Lock()
        # Resolved once, here rather than per write. The adapter unit tests reassign it to
        # their own error class — the seam that lets the recovery path be exercised off-Pi.
        self._alsa_error: Any = _alsa_error_type()

    async def play(self, chunk: AudioChunk) -> int:
        """Write one chunk on a worker thread (P8) at **the chunk's own format**; return the ms
        the device accepted. Clears any prior barge-in first, so a fresh utterance resumes after
        a :meth:`stop`."""
        self._stopped.clear()
        return await asyncio.to_thread(
            self._write_all, chunk.pcm, chunk.sample_rate, chunk.channels
        )

    async def play_file(self, path: str | Path) -> int:
        """Play a canned WAV at the file's own format, in ~20 ms period writes, breaking the
        moment a barge-in :meth:`stop` fires (SDS §3.10). Returns the ms the device accepted —
        short when a barge-in truncated the clip, which is a fact rather than an error."""
        self._stopped.clear()
        pcm, rate, channels = await asyncio.to_thread(_read_wav, Path(path))
        return await asyncio.to_thread(self._write_all, pcm, rate, channels)

    async def stop(self) -> None:
        """Barge-in: set the interrupt flag (immediately, no thread hop, no lock) and close the
        playback handle on a thread so ALSA drops its buffered audio (SDS §3.10)."""
        self._stopped.set()
        await asyncio.to_thread(self._close_blocking)

    # --- worker thread: everything below blocks and must never run on the loop (P8) -----

    def _write_all(self, pcm: bytes, rate: int, channels: int) -> int:
        """Write *pcm* in ~20 ms periods at ``(rate, channels)``; return the ms **accepted**.

        Holds the lock for the whole loop (see the class docstring) and re-checks the barge-in
        flag between periods, so a :meth:`stop` truncates within one period rather than after
        the utterance. Returns short — possibly zero — when periods were dropped or truncated;
        the caller decides whether that is worth a log against its correlation id."""
        period = _period_bytes(rate, channels)
        accepted_frames = 0
        with self._lock:
            for index, start in enumerate(range(0, len(pcm), period)):
                if self._stopped.is_set():
                    break
                accepted_frames += self._write_period_locked(
                    pcm[start : start + period], rate, channels, index=index
                )
        return frames_duration_ms(accepted_frames, sample_rate=rate)

    def _write_period_locked(
        self, data: bytes, rate: int, channels: int, *, index: int
    ) -> int:
        """Write one period; return the frames the device took. Caller holds ``_lock``.

        On an underrun the handle is closed, reopened and the period retried **once**: ALSA
        fails the write after the stream has drained, and the Pi evidence is that a fresh
        handle never drops (AVID-91). ``pyalsaaudio`` exposes no ``prepare()``, so close-and-
        reopen *is* the recovery. An XRUN at an utterance boundary is expected rather than
        alarming, hence DEBUG; only a period that fails twice, or lands short, is a WARNING."""
        frames = len(data) // max(1, channels * SAMPLE_WIDTH_BYTES)
        accepted = self._try_write_locked(data, rate, channels)
        if accepted is None:
            _log.debug(
                "speaker underrun on period %d at %d Hz; reopening the device",
                index,
                rate,
            )
            self._close_locked()
            accepted = self._try_write_locked(data, rate, channels)
        if accepted is None:
            _log.warning(
                "speaker dropped period %d (%d frames at %d Hz): recovery failed",
                index,
                frames,
                rate,
            )
            return 0
        accepted = min(accepted, frames)
        if accepted < frames:
            _log.warning(
                "speaker took %d of %d frames on period %d at %d Hz",
                accepted,
                frames,
                index,
                rate,
            )
        return accepted

    def _try_write_locked(self, data: bytes, rate: int, channels: int) -> int | None:
        """One ``write()``. ``None`` means underrun — the library reports it as a negative
        return (``-32``/``-EPIPE``), a zero return, or a raised ``ALSAAudioError`` depending on
        build and mode, and all three mean the same thing: nothing was played. Caller holds
        ``_lock``."""
        try:
            written = int(self._ensure_open_locked(rate, channels).write(data))
        except self._alsa_error:
            return None
        return written if written > 0 else None

    def _ensure_open_locked(self, rate: int, channels: int) -> Any:
        """The handle open at ``(rate, channels)``, reopening it if the format differs.

        Rate changes only happen between utterances (a 24 kHz reply, a 16 kHz echo, a cue at
        the file's own rate), so the reopen cost is irrelevant. Caller holds ``_lock``."""
        if self._pcm is not None and (self._open_rate, self._open_channels) == (
            rate,
            channels,
        ):
            return self._pcm
        self._close_locked()
        self._note_format(rate, channels)
        self._pcm = self._open_blocking(rate, channels)
        self._open_rate, self._open_channels = rate, channels
        return self._pcm

    def _note_format(self, rate: int, channels: int) -> None:
        """Announce, once, that the audio's format is not the configured nominal one.

        One line per adapter lifetime, at INFO: it is normal (the M4 loopback echoes 16 kHz
        capture through a 24 kHz-nominal speaker) but it is exactly what a silently-wrong rig
        looks like, and this is the grep that would have caught AVID-91 in a log rather than
        in a room three weeks later."""
        if self._nominal_warned or (rate, channels) == (
            self._nominal_rate,
            self._nominal_channels,
        ):
            return
        self._nominal_warned = True
        _log.info(
            "speaker opened at %d Hz / %dch; nominal format is %d Hz / %dch "
            "(honouring the chunk)",
            rate,
            channels,
            self._nominal_rate,
            self._nominal_channels,
        )

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

    def _close_locked(self) -> None:
        """Close the handle and forget its format. Caller holds ``_lock``."""
        if self._pcm is not None:
            self._pcm.close()
            self._pcm = None
            self._open_rate = self._open_channels = None

    def _close_blocking(self) -> None:
        """:meth:`stop`'s close, on a worker thread.

        The flag re-check is not redundant: ``stop`` signals synchronously but closes on a
        thread hop, so a :meth:`play` starting in between will already have cleared the flag —
        and that fresh utterance owns the handle. Closing it here would kill the audio that
        *replaced* what the user barged in on."""
        with self._lock:
            if not self._stopped.is_set():
                return
            self._close_locked()
