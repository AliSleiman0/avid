"""Measure YuNet's per-frame cost on the Pi, to settle ADR-013's estimate and #221 AC-7.

ADR-013 shipped with an *expectation* — "≲40 ms per frame at 640×480 single-threaded" — and
said in as many words that #221 replaces it with a measurement. This is that measurement, and
the expectation was wrong by a factor of three. §2.7.1 budgets **≤1 core at ≤5 fps**, so the
frame period is 200 ms and capture shares the same thread as inference (§3.8.2): one number
decides whether the milestone's headline resource criterion is reachable at all.

**Why a probe rather than the gate run.** The question — how long does one inference take, at
what resolution, on how many threads — needs no camera, no service and no human. Answering it
with a bench run would cost an afternoon and confound inference cost with capture, the bus and
the display. This finishes in two minutes and produces a table. Same discipline
``probe_first_token.py`` paid for: measure the model before tuning around it (CLAUDE.md §3).

⚠️ **What this measures and what it does not.** It measures `session.run` on synthetic input:
inference only. It excludes camera capture, the numpy preprocessing and the box decode, all of
which the service pays on the same thread — so these figures are a **floor** for the loop's
per-frame cost, not a prediction of it. It also says nothing about *accuracy*: a resolution
that is fast and cannot see a face at desk distance is not a candidate, and only a run against
a real face decides that.

⚠️ **YuNet's strides are 8/16/32, so both input dimensions must be divisible by 32.** A size
that is not is silently unavailable rather than slow — the run simply rejects it, which is why
every candidate here is checked before it is timed. 640×480 already satisfies this; 320×240
(the obvious 2× decimation) does **not**, and has to be padded to 320×256.

Run on the Pi, against models fetched by ``tools/fetch_face_model.py``::

    /opt/avid/.venv/bin/python tools/probe_face_detector.py
    /opt/avid/.venv/bin/python tools/probe_face_detector.py --models-dir /tmp/yunet --trials 25

A **dev/provisioning-only** tool: not imported by the application, not run in CI, and outside
``avid/`` so it is clear of mypy and coverage (the lint job still formats it).
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
import time
from pathlib import Path
from typing import Any

# Where tools/fetch_face_model.py installs the blob, and where the adapter looks.
_DEFAULT_MODELS_DIR = Path("/var/lib/robot/models")

# YuNet's stride set. Both input dimensions must be divisible by the largest one.
_STRIDE = 32

# (height, width) candidates, largest first. 640x480 is the rig's negotiated capture geometry
# (ov5647, contract-proven at M2) and needs no resampling at all; the rest are exact 2x box
# decimations of it, bottom-padded to reach a multiple of the stride.
_SIZES: tuple[tuple[int, int], ...] = ((480, 640), (384, 512), (256, 320), (192, 256))

# 1 is the budget's answer and 2 is the honest control: SileroVad wants 1 thread and
# LocalMiniLmEmbedder wants 2, and vad.py's comment tells any third ONNX adapter to *measure
# its own number, never paste the 1*. So both are measured, with cores-busy alongside latency —
# a thread count that halves latency while doubling cores has not helped a ≤1-core budget.
_THREADS: tuple[int, ...] = (1, 2)

_FRAME_PERIOD_MS = 200.0  # 5 fps, SDS §2.7.1


def _session(path: Path, threads: int) -> Any:
    """An ONNX session under the treatment the ≤1-core budget requires (SDS §3.8.2).

    Identical to ``SileroVad``'s and ``LocalMiniLmEmbedder``'s, because measuring under
    *default* session options would measure a configuration that must never ship: ONNX Runtime
    spins one thread per core between inferences, which is how the robot went deaf at M5.
    """
    import onnxruntime

    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.execution_mode = onnxruntime.ExecutionMode.ORT_SEQUENTIAL
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return onnxruntime.InferenceSession(
        str(path), sess_options=options, providers=["CPUExecutionProvider"]
    )


def _measure(
    path: Path, size: tuple[int, int], threads: int, trials: int
) -> dict[str, Any] | None:
    """Median/min/max inference ms and cores busy, or ``None`` if the model rejects *size*."""
    import numpy as np

    height, width = size
    if height % _STRIDE or width % _STRIDE:
        return None
    session = _session(path, threads)
    name = session.get_inputs()[0].name
    frame = np.random.rand(1, 3, height, width).astype(np.float32)
    try:
        for _ in range(3):  # warm the graph; the first run allocates
            session.run(None, {name: frame})
    except Exception:
        # A statically-shaped export refuses anything but its own geometry. That is a fact
        # about the model, not a failure of the probe — report it as unavailable.
        return None

    before = resource.getrusage(resource.RUSAGE_SELF)
    wall_start = time.perf_counter()
    samples: list[float] = []
    for _ in range(trials):
        started = time.perf_counter()
        session.run(None, {name: frame})
        samples.append((time.perf_counter() - started) * 1000.0)
    wall = time.perf_counter() - wall_start
    after = resource.getrusage(resource.RUSAGE_SELF)
    cpu = (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)

    median = statistics.median(samples)
    return {
        "model": path.name,
        "height": height,
        "width": width,
        "threads": threads,
        "median_ms": round(median, 1),
        "min_ms": round(min(samples), 1),
        "max_ms": round(max(samples), 1),
        # Cores busy over the measured window. The honest companion to latency: this is the
        # number §2.7.1 caps at 1, and a thread count that buys speed by burning cores has not
        # helped. Note it counts only the process, which is what the budget is about.
        "cores": round(cpu / wall, 2) if wall > 0 else 0.0,
        # What fraction of a 5 fps frame period this inference alone consumes, BEFORE capture,
        # preprocessing and the box decode, which the same thread also pays.
        "budget_frac": round(median / _FRAME_PERIOD_MS, 2),
    }


def _describe_inputs(path: Path) -> str:
    """The model's declared input shape — the thing that decides whether resizing is needed.

    Worth printing rather than assuming: two exports of the same model in the same directory
    differ here. ``2023mar`` is statically ``[1, 3, 640, 640]`` and rejects the rig's 640×480
    outright; ``2026may`` declares ``[1, 3, 'height', 'width']`` and takes it natively.
    """
    session = _session(path, 1)
    spec = session.get_inputs()[0]
    return f"{spec.name} {spec.shape}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=_DEFAULT_MODELS_DIR,
        help=f"directory holding the .onnx blobs (default: {_DEFAULT_MODELS_DIR})",
    )
    parser.add_argument(
        "--trials", type=int, default=15, help="timed inferences per cell (default: 15)"
    )
    parser.add_argument("--json", type=Path, help="also write the rows here")
    args = parser.parse_args()

    models = sorted(args.models_dir.glob("face_detection_yunet*.onnx"))
    if not models:
        print(
            f"no face_detection_yunet*.onnx under {args.models_dir} — "
            f"run `python tools/fetch_face_model.py` first",
            file=sys.stderr,
        )
        return 1

    rows: list[dict[str, Any]] = []
    for path in models:
        print(f"\n{path.name}  input: {_describe_inputs(path)}")
        print(
            f"  {'size':<10}{'thr':<5}{'median ms':>10}{'min':>8}{'max':>8}{'cores':>7}{'of 200ms':>10}"
        )
        for size in _SIZES:
            for threads in _THREADS:
                row = _measure(path, size, threads, args.trials)
                if row is None:
                    continue
                rows.append(row)
                label = f"{row['height']}x{row['width']}"
                print(
                    f"  {label:<10}{threads:<5}{row['median_ms']:>10.1f}"
                    f"{row['min_ms']:>8.1f}{row['max_ms']:>8.1f}"
                    f"{row['cores']:>7.2f}{row['budget_frac']:>10.2f}"
                )
            if not any(r["height"] == size[0] and r["width"] == size[1] for r in rows):
                print(
                    f"  {size[0]}x{size[1]:<5} unavailable (the export rejects this geometry)"
                )

    if args.json:
        args.json.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nwrote {len(rows)} rows to {args.json}")
    print(
        "\nReminder: inference only. Capture, preprocessing and the box decode share the same "
        "thread (§3.8.2), so the loop's real per-frame cost is above every figure here."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
