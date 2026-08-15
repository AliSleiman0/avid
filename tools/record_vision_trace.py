"""Record a per-frame detection trace on the Pi, for #225's committed flapping regression.

The M8 gate says *"no flapping over a 1-hour desk recording"*, and that criterion has an
awkward property: the evidence is an hour of video, which is gigabytes. The largest tracked
file in this repo is **566 KB** and there is no LFS, so the recording cannot be the artefact.
**The trace can.** Record the hour here, run the real detector over it live, and commit the
derived per-frame trace — timestamp, count, top confidence, largest box, one row per frame. At
At 5 fps an hour is 18,000 rows. Measured on the Pi: an empty-room row is 24 bytes and a
row carrying a face is ~53, so an hour lands between **~420 KB and ~930 KB** depending on how
much of it was occupied — the same order as ``assets/eval/`` and small enough to live in git
without LFS, which a video never could be.

Then, because the hysteresis filter is a **pure function**, CI replays a real hour of desk
activity through it on every build, on a laptop, with no camera. That is the difference between
the gate's headline property being *true once on one afternoon* and being permanently defended.
It is the same move ``assets/sessions/`` already makes for the Realtime API.

⚠️ **The video never leaves the Pi — it is never even written.** This tool holds no frame
beyond the one it is judging: it detects, writes four numbers, and drops the pixels. What is
committed is bounding boxes and confidences, **not images** (SDS §13). Recording a room is a
privacy-relevant act even when the pixels are discarded, so say where and when in the header.

⚠️ **Reprovision before recording.** *The machine is not the repo*: ``/etc/robot/config.toml``
is a copy that rots, and a key missing from it falls back to a schema default **silently**, so
an hour recorded under stale settings is an hour recorded against the wrong thresholds and
nothing will say so (``deploy/PI_OPERATIONS.md`` §3). This tool prints the settings it loaded
before it records the first frame, so the header of the run is the evidence.

⚠️ **Stop ``robot.service`` first** — it holds the camera (``PI_OPERATIONS`` §1).

Run on the Pi::

    sudo systemctl stop robot
    /opt/avid/.venv/bin/python tools/record_vision_trace.py \\
        --config /etc/robot/config.toml --minutes 60 \\
        --out assets/vision/desk_hour.jsonl --label "afternoon desk, office light"

A **dev/provisioning-only** tool: not imported by the application, not run in CI, and outside
``avid/`` so it is clear of mypy and coverage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from avid.adapters.camera import Picamera2Camera  # noqa: E402
from avid.adapters.face_detector import OnnxFaceDetector  # noqa: E402
from avid.core.config import load_config  # noqa: E402


async def _record(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    vision = config.vision
    period_s = 1.0 / vision.fps

    # Print what was LOADED, never what the tool assumes. A banner quoting a literal is drift
    # with a delay fuse (CLAUDE.md §7.1), and a config key missing from the machine's copy
    # falls back to a schema default without a word.
    print(f"config          {args.config}")
    print(
        f"  camera        {config.camera.width}x{config.camera.height} @ {config.camera.fps} fps"
    )
    print(
        f"  adapters      camera={config.adapters.camera} face_detector={config.adapters.face_detector}"
    )
    print(f"  vision.fps    {vision.fps}   detector_scale {vision.detector_scale}")
    print(f"  threshold     {vision.confidence_threshold}")
    print(
        f"  windows       gain {vision.gain_window_s}s / lose {vision.lose_window_s}s"
    )
    print(f"  nap_after_s   {vision.nap_after_s}")
    if (
        config.adapters.face_detector != "yunet"
        or config.adapters.camera != "picamera2"
    ):
        print(
            "\nREFUSING: this must record the REAL camera through the REAL detector. "
            "FakeCamera + FakeFaceDetector compose into a convincing simulator that consumes "
            "no CPU and never mis-detects, which is precisely why a trace from them proves "
            "nothing (#226's notes).",
            file=sys.stderr,
        )
        return 2

    camera = Picamera2Camera(
        width=config.camera.width, height=config.camera.height, fps=config.camera.fps
    )
    detector = OnnxFaceDetector(scale=vision.detector_scale)
    await camera.start()

    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    frames = int(args.minutes * 60 * vision.fps)
    started_wall = time.time()
    started_ns = time.monotonic_ns()
    errors = 0

    header: dict[str, Any] = {
        "_meta": {
            "purpose": "Per-frame detection trace for the M8 flapping regression (#225).",
            "contains": "timestamps, face counts, confidences and bounding boxes. NO IMAGES.",
            "label": args.label,
            "recorded_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_wall)
            ),
            "model": "face_detection_yunet_2026may.onnx (ADR-013)",
            "camera": f"{config.camera.width}x{config.camera.height} RGB888, ov5647 (Pi Camera v1)",
            "detector_scale": vision.detector_scale,
            "fps": vision.fps,
            # The thresholds in force AT RECORDING TIME. The trace is replayed through the
            # filter with whatever config the test chooses, so these are provenance rather than
            # parameters — but a trace whose recording thresholds are unknown cannot be
            # re-derived, and #226 may need to.
            "confidence_threshold_at_recording": vision.confidence_threshold,
            "gain_window_s_at_recording": vision.gain_window_s,
            "lose_window_s_at_recording": vision.lose_window_s,
            "schema": "one compact JSON object per line after this header: "
            "{t: seconds since start (monotonic), n: face count, c: top confidence, "
            "box: [x, y, w, h] of the largest face}. c and box are OMITTED when n is 0 "
            "(most rows in a real hour), and a frame that could not be judged carries "
            "{t, error} instead of n - an error is not an absence.",
        }
    }

    print(f"\nrecording {frames} frames (~{args.minutes:.0f} min) -> {out}")
    print(
        "behave ordinarily: sit, type, lean out of frame, turn away, leave, come back.\n"
    )

    try:
        with out.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(header) + "\n")
            for index in range(frames):
                tick_ns = time.monotonic_ns()
                try:
                    frame = await camera.capture()
                    detections = await detector.detect(frame)
                except Exception as exc:  # a fault is not evidence of absence
                    errors += 1
                    row = {
                        "t": round((tick_ns - started_ns) / 1e9, 3),
                        "error": type(exc).__name__,
                    }
                else:
                    best = max(detections, key=lambda d: d.confidence, default=None)
                    largest = max(detections, key=lambda d: d.box.area, default=None)
                    row = {
                        "t": round((tick_ns - started_ns) / 1e9, 3),
                        "n": len(detections),
                    }
                    # ``c`` and ``box`` are omitted on an empty frame rather than written as
                    # 0.0/null. Measured on the Pi, omitting them plus compact separators
                    # takes an empty-room row from 46 bytes to 24 — an hour from ~814 KB to
                    # ~424 KB. Worth doing when the repo has no LFS. The loader defaults them,
                    # so their absence reads as "nothing seen" and never as "unknown".
                    if best is not None and largest is not None:
                        row["c"] = round(best.confidence, 4)
                        row["box"] = [
                            largest.box.x,
                            largest.box.y,
                            largest.box.w,
                            largest.box.h,
                        ]
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")

                if index % (vision.fps * 60) == 0 and index:
                    handle.flush()
                    print(f"  {index // (vision.fps * 60):>3} min, {errors} error(s)")

                elapsed = (time.monotonic_ns() - tick_ns) / 1e9
                await asyncio.sleep(max(0.0, period_s - elapsed))
    finally:
        await camera.stop()

    wall = time.monotonic_ns() - started_ns
    # ASYNC240: a blocking Path call inside an async function. The camera is stopped and
    # nothing else is on the loop by now, so it is a false positive here — but hopping the
    # thread costs nothing and keeps the rule loud everywhere it is not.
    size_kb = (await asyncio.to_thread(out.stat)).st_size // 1024
    print(
        f"\nwrote {frames} rows in {wall / 1e9 / 60:.1f} min "
        f"({errors} error frames) -> {out} ({size_kb} KB)"
    )
    print(
        "Now annotate the real arrivals and departures in "
        "assets/vision/ground_truth.json — without them 'no flapping' is unfalsifiable, "
        "because a filter reporting that nobody was ever here also never flaps (#225 AC-4)."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/robot/config.toml"))
    parser.add_argument("--minutes", type=float, default=60.0)
    parser.add_argument(
        "--out", type=Path, default=Path("assets/vision/desk_hour.jsonl")
    )
    parser.add_argument(
        "--label",
        default="",
        help="the conditions, in words — lighting, time of day, what the room is (provenance)",
    )
    args = parser.parse_args()
    return asyncio.run(_record(args))


if __name__ == "__main__":
    raise SystemExit(main())
