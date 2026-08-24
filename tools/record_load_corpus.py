#!/usr/bin/env python
"""Record the soak load corpus, one utterance at a time (#389).

The generator needs recorded human speech, and the format is fussy in ways that are easy to get
wrong by hand: **16 kHz, mono, 16-bit PCM**. It has to be 16 kHz because that is what the robot's
microphone captures and what Silero supports, and a 44.1 kHz recording cannot be converted down
afterwards — the repo's resampler refuses to downsample without an anti-alias filter, deliberately.
So this records at the right rate in the first place and there is nothing to convert.

    uv run --frozen --with sounddevice python tools/record_load_corpus.py

It reads ``assets/load/manifest.json``, prompts for each line in turn, and writes the filename the
manifest expects. Press Enter to start a clip, Enter again to stop. Anything already recorded is
skipped unless you pass ``--redo``.

Bench tool, not application code: it lives in ``tools/`` outside P3's composition root, and
``sounddevice`` is deliberately NOT a project dependency — it is needed once, on a laptop, to make
an asset.
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

RATE = 16000  # the microphone's rate, and one of the two Silero supports
CHANNELS = 1
WIDTH = 2  # 16-bit PCM


def _load_manifest(corpus: Path) -> list[dict[str, str]]:
    data = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    return list(data["utterances"])


def _record_one(target: Path) -> float:
    """Record until the operator presses Enter again. Returns the duration in seconds."""
    import queue

    import sounddevice as sd

    frames: queue.Queue[bytes] = queue.Queue()

    def callback(indata, _frames, _time, status) -> None:  # type: ignore[no-untyped-def]
        if status:
            print(f"  ({status})", flush=True)
        frames.put(bytes(indata))

    with sd.RawInputStream(
        samplerate=RATE, channels=CHANNELS, dtype="int16", callback=callback
    ):
        input()

    pcm = b""
    while not frames.empty():
        pcm += frames.get()

    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(WIDTH)
        handle.setframerate(RATE)
        handle.writeframes(pcm)
    return len(pcm) / float(RATE * CHANNELS * WIDTH)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", default="assets/load")
    parser.add_argument(
        "--redo", action="store_true", help="re-record clips that already exist"
    )
    parser.add_argument(
        "--list-devices", action="store_true", help="show input devices and exit"
    )
    args = parser.parse_args()

    try:
        import sounddevice as sd
    except ImportError:
        print(
            "sounddevice is not installed. Run this as:\n\n"
            "    uv run --frozen --with sounddevice python tools/record_load_corpus.py\n"
        )
        return 2

    if args.list_devices:
        print(sd.query_devices())
        return 0

    corpus = Path(args.corpus)
    utterances = _load_manifest(corpus)

    print("=" * 70)
    print(f"Recording {len(utterances)} clips into {corpus}  ({RATE} Hz mono 16-bit)")
    print("Press Enter to START a clip, then Enter again to STOP.")
    print("Say the line naturally, and stop as soon as you finish the sentence.")
    print("Ctrl-C to quit; anything already recorded is kept.")
    print("=" * 70)

    for i, utterance in enumerate(utterances, 1):
        target = corpus / str(utterance["file"])
        if target.exists() and not args.redo:
            print(
                f"\n[{i}/{len(utterances)}] {target.name} — already recorded, skipping"
            )
            continue
        print(f"\n[{i}/{len(utterances)}] {target.name}")
        print(f'    say:  "{utterance["text"]}"')
        input("    Enter to start recording... ")
        print("    RECORDING — Enter to stop.", flush=True)
        try:
            seconds = _record_one(target)
        except KeyboardInterrupt:
            print("\n  stopped")
            return 0
        print(f"    saved {target.name}  ({seconds:.2f}s)")

    print("\nAll done. Now check them:\n")
    print(
        "    uv run --frozen python docs/demos/soak_load.py --mode validate "
        f"--corpus {corpus} --config config/sim.toml\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
