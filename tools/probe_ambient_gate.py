#!/usr/bin/env python
"""Measure what a high-pass does to a recording — to the *level*, and to the **VAD** (AVID-283).

The defect is that a 50 Hz mains hum defeats the voice gate. The rig, in an empty silent room,
measured:

    raw ambient              -18.4 dBFS     <- what the gate sees
    high-passed at 200 Hz    -43.0 dBFS
    speech band 300-3400 Hz  -49.1 dBFS     <- what a human calls silence

⚠️ **The obvious reading of that is wrong, and this script exists because of it.** The handoff
concluded that since ``rms_dbfs`` has exactly one caller, filtering there fixes the problem
everywhere. It does not. Reading the mic loop:

* ``AudioService._admits_barge_in`` returns ``True`` immediately when the uplink is not shut, so
  the echo floor is consulted **only while the robot is speaking**.
* ``EchoFloor.observe`` is called from the **idle** branch.
* ``self._vad.is_speech(chunk)`` runs *before* the level measurement and is handed the whole raw
  frame; ``SileroVad`` does no level gating and no pre-filtering.

So the corrupted floor is real, but it cannot be what opens a phantom session. **Silero firing on
the hum is.** Filtering only the level measurement would make the number in the log look right and
leave the nine dropped turns exactly where they are — a fix that measures better without working,
which is precisely the M8 failure this project has already paid for once.

Hence the last column. For each candidate ``(cutoff, order)`` this reports the level *and* how
many frames the real Silero VAD calls speech. That count is what says where the filter belongs,
and no amount of reasoning substitutes for it.

    # off-Pi, level columns only (no onnxruntime needed)
    uv run --frozen python tools/probe_ambient_gate.py amb2.wav --no-vad

    # on the Pi, the column that matters
    /opt/avid/.venv/bin/python tools/probe_ambient_gate.py ~/vision_traces/logs/amb2.wav

⚠️ **A silent room cannot validate this fix.** A filter that flattens the floor while also gating
out real speech would look perfect here. Run it over a recording with someone talking too, and
read the two together: the hum count should collapse and the speech count must not.

Bench tool, not application code — it lives in ``tools/`` outside P3's composition root, and
builds its own objects.
"""

from __future__ import annotations

import argparse
import math
import sys
import wave
from array import array
from pathlib import Path

from avid.core.hal import AudioChunk
from avid.domain import HighPass, rms_dbfs

# The band SDS §6.3 treats as speech. Energy outside it is never the user, which is the entire
# premise of AVID-283 — so it is reported alongside the broadband number on every row.
_SPEECH_LOW_HZ = 300.0
_SPEECH_HIGH_HZ = 3400.0

# (cutoff_hz, order) pairs to sweep. Deliberately spans one pole (too weak, ~10 dB at 50 Hz) to
# four, so the table shows the trade rather than asserting a chosen answer.
_CANDIDATES = [
    (150.0, 1),
    (150.0, 2),
    (150.0, 3),
    (150.0, 4),
    (200.0, 2),
    (200.0, 3),
]


def _load_wav(path: Path) -> tuple[bytes, int]:
    """Read a mono 16-bit WAV as raw PCM plus its sample rate."""
    with wave.open(str(path), "rb") as handle:
        if handle.getsampwidth() != 2:
            raise SystemExit(
                f"{path}: expected 16-bit PCM, got {handle.getsampwidth() * 8}-bit"
            )
        if handle.getnchannels() != 1:
            raise SystemExit(
                f"{path}: expected mono, got {handle.getnchannels()} channels"
            )
        return handle.readframes(handle.getnframes()), handle.getframerate()


def _band_dbfs(pcm: bytes, *, sample_rate: int, low_hz: float, high_hz: float) -> float:
    """Level inside a frequency band, by direct Goertzel-style summation.

    A full FFT would need numpy, which is fine in ``tools/`` but pointless here: this reads one
    band once, and correlating against a bin's sine and cosine is a dozen lines of stdlib. Bins
    are spaced at the analysis resolution below and summed as power, so the result is comparable
    to :func:`rms_dbfs` on the same signal.
    """
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    count = len(samples)
    if count == 0:
        return -120.0

    # ~25 Hz resolution is plenty to separate 50 Hz hum from a 300 Hz speech edge.
    step_hz = 25.0
    power = 0.0
    freq = low_hz
    while freq <= high_hz:
        omega = 2.0 * math.pi * freq / sample_rate
        real = math.fsum(s * math.cos(omega * n) for n, s in enumerate(samples))
        imag = math.fsum(s * math.sin(omega * n) for n, s in enumerate(samples))
        power += (real * real + imag * imag) / (count * count) * 2.0
        freq += step_hz
    if power <= 0.0:
        return -120.0
    return 20.0 * math.log10(math.sqrt(power) / 32768.0)


def _dominant_hz(pcm: bytes, *, sample_rate: int) -> float:
    """The loudest bin below 500 Hz — the number that identified this as mains hum, not room noise."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    count = len(samples)
    if count == 0:
        return 0.0
    best_hz, best_power = 0.0, -1.0
    freq = 10.0
    while freq <= 500.0:
        omega = 2.0 * math.pi * freq / sample_rate
        real = math.fsum(s * math.cos(omega * n) for n, s in enumerate(samples))
        imag = math.fsum(s * math.sin(omega * n) for n, s in enumerate(samples))
        power = real * real + imag * imag
        if power > best_power:
            best_hz, best_power = freq, power
        freq += 5.0
    return best_hz


def _speech_frames(
    pcm: bytes, *, sample_rate: int, chunk_ms: int, threshold: float
) -> int | None:
    """How many frames the real Silero VAD calls speech. ``None`` if onnxruntime is unavailable.

    The real adapter, not a reimplementation: a probe that models the VAD would be measuring the
    model of the model. Returns ``None`` rather than guessing off-Pi, so a missing dependency
    reads as *not measured* instead of as zero — a zero here would be the most misleading possible
    output, since zero speech frames is exactly what a working filter looks like.
    """
    try:
        from avid.adapters.vad import SileroVad
    except ImportError:  # pragma: no cover - bench tool
        return None
    try:
        vad = SileroVad(sample_rate=sample_rate, threshold=threshold)
        frame_bytes = sample_rate * 2 * chunk_ms // 1000
        speech = 0
        for start in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
            chunk = AudioChunk(
                pcm=pcm[start : start + frame_bytes],
                sample_rate=sample_rate,
                channels=1,
            )
            if vad.is_speech(chunk):
                speech += 1
        return speech
    except Exception as exc:  # noqa: BLE001 - a bench tool reports, it does not crash
        print(f"  (VAD unavailable: {type(exc).__name__}: {exc})", file=sys.stderr)
        return None


def _row(
    label: str,
    pcm: bytes,
    *,
    sample_rate: int,
    chunk_ms: int,
    threshold: float,
    run_vad: bool,
) -> None:
    broadband = rms_dbfs(pcm)
    speech_band = _band_dbfs(
        pcm, sample_rate=sample_rate, low_hz=_SPEECH_LOW_HZ, high_hz=_SPEECH_HIGH_HZ
    )
    frames = (
        _speech_frames(
            pcm, sample_rate=sample_rate, chunk_ms=chunk_ms, threshold=threshold
        )
        if run_vad
        else None
    )
    shown = "not measured" if frames is None else str(frames)
    print(f"{label:<18} {broadband:>10.1f} {speech_band:>12.1f} {shown:>14}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path, help="mono 16-bit WAV to measure")
    parser.add_argument(
        "--chunk-ms",
        type=int,
        default=20,
        help="frame size, matching [microphone] chunk_ms",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.5,
        help="Silero speech probability cutoff, matching [gate] threshold",
    )
    parser.add_argument(
        "--no-vad",
        action="store_true",
        help="skip the VAD column (it needs onnxruntime and the model blob)",
    )
    args = parser.parse_args(argv)

    pcm, sample_rate = _load_wav(args.wav)
    duration_s = len(pcm) / 2 / sample_rate
    total_frames = int(duration_s * 1000 // args.chunk_ms)

    print(f"{args.wav} - {duration_s:.1f} s at {sample_rate} Hz, {total_frames} frames")
    print(
        f"dominant frequency below 500 Hz: {_dominant_hz(pcm, sample_rate=sample_rate):.0f} Hz"
    )
    print()
    print(f"{'filter':<18} {'broadband':>10} {'300-3400 Hz':>12} {'VAD speech':>14}")
    print(f"{'-' * 18} {'-' * 10} {'-' * 12} {'-' * 14}")

    _row(
        "raw",
        pcm,
        sample_rate=sample_rate,
        chunk_ms=args.chunk_ms,
        threshold=args.threshold,
        run_vad=not args.no_vad,
    )
    for cutoff, order in _CANDIDATES:
        filt = HighPass(cutoff_hz=cutoff, sample_rate=sample_rate, order=order)
        # Filtered frame by frame, exactly as AudioService would — the filter is stateful, and
        # applying it to the whole recording in one call would measure a startup transient the
        # running robot never sees.
        frame_bytes = sample_rate * 2 * args.chunk_ms // 1000
        filtered = b"".join(
            filt.apply(pcm[start : start + frame_bytes])
            for start in range(0, len(pcm) - frame_bytes + 1, frame_bytes)
        )
        _row(
            f"{cutoff:g} Hz x{order}",
            filtered,
            sample_rate=sample_rate,
            chunk_ms=args.chunk_ms,
            threshold=args.threshold,
            run_vad=not args.no_vad,
        )

    print()
    print(
        "Read the VAD column, not the level columns: the level is what the echo floor sees while\n"
        "the robot is SPEAKING, but a phantom session is opened by the VAD on an IDLE robot. A row\n"
        "with a good level and a nonzero speech count has not fixed the reported defect.\n"
        "WARNING: a zero speech count over a SILENT room proves only half of it. The other half\n"
        "is a recording with a person talking, where this count must stay high."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
