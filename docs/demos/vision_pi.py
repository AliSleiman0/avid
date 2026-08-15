"""On-Pi M8 gate harness (#226) — measure the resources vision actually costs.

M8's two headline criteria are **resource** criteria, and resource criteria are exactly what a
green test suite cannot see. Every unit test in this milestone passes on a laptop where nothing
is thermally constrained and four cores are idle. §2.7.1's budget — *"Vision must not exceed 1
core; run detection at ≤5 fps, not 30"* — is only meaningful measured on this hardware, with the
camera running, while the robot is doing everything else it does.

And the project has a specific, twice-repeated way to fail it: ONNX Runtime spins one thread per
core by default. Silero pegged 3 of 4 and **the robot went deaf**; the MiniLM embedder still pegs
3.92 of 4 (#168). If #221's SessionOptions treatment were missed, this is where it would show up
— as *"vision is too expensive"* rather than as the two-line mistake it actually is.

What this measures, and what it deliberately does not:

* **cpu (AC-1)** — per-process *and* per-thread CPU over a sustained run, read from
  ``/proc/<pid>/stat`` and ``/proc/<pid>/task/*/stat``. Per-thread is the half that matters: a
  second executor or a spinning ONNX pool is invisible in a process total that is under budget
  for other reasons, and obvious the moment the threads are listed.
* **fps (AC-2)** — the rate the capture loop *achieved*, from the service's own frame counter
  over wall time. If detection cannot keep up, that is **a finding to record, not a number to
  quietly lower** (the issue says so explicitly).
* **thermals (AC-3)** — the SoC temperature curve and ``vcgencmd get_throttled``. A Pi that
  throttles during vision degrades the audio path too, and that coupling is why the criterion
  exists at all.
* **events (AC-4)** — every ``vision.*`` fact published during the run, counted and timestamped,
  so the flapping question is answered against the *whole* path (camera → detector → filter →
  bus) rather than against #225's replay of the filter alone.

**What it cannot measure, and says so rather than implying otherwise:** whether the robot woke
when a person sat down (AC-5), whether the nap cancelled on their return (AC-6), whether a
conversation still sounds right (AC-7), or how the detector behaves under different light
(AC-8). Those need a human in the room, and a harness that scored them from an empty one would
be the M4 lesson repeating — *a gate that can pass on silence is not a gate*. They are reported
as **manual**, and the exit code refuses to call the gate sealed while any of them is unrecorded.

⚠️ **A stimulus the harness induces is not a measurement of the robot.** This runs the real
composition root against the real config and only *observes*: it publishes nothing, injects no
frames, and never touches the filter's state. The numbers are what the robot did.

Usage on the Pi (service **stopped**, per PI_OPERATIONS §1)::

    /opt/avid/.venv/bin/python docs/demos/vision_pi.py --config /etc/robot/config.toml \\
        --minutes 20 --json docs/demos/m8_evidence/gate.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from avid.core.config import Config, load_config  # noqa: E402
from avid.core.event_bus import AsyncioEventBus  # noqa: E402
from avid.domain import Event  # noqa: E402
from avid.domain.vision import (  # noqa: E402
    VisionFaceDetected,
    VisionPresenceGained,
    VisionPresenceLost,
)

# SDS §2.7.1. Stated once, here, and compared against — never restated in a print (§7.1).
_CORE_BUDGET = 1.0
# Above this the SoC is close enough to throttling to be worth naming even if it has not.
_TEMP_WATCH_C = 75.0
_CLOCK_TICKS = 100  # USER_HZ on Linux; /proc/*/stat counts in these


_Verdict = Literal["pass", "fail", "recorded", "manual"]


@dataclass
class _Criterion:
    """One reported line. Held rather than printed so **every criterion reports before any
    verdict is decided** — hiding a passing check behind an unrelated failure is the sibling of
    passing on silence (CLAUDE.md §7.1)."""

    ac: str
    name: str
    verdict: _Verdict
    detail: str = ""
    rows: list[str] = field(default_factory=list)


# ── /proc sampling ───────────────────────────────────────────────────────────────────────────


def _proc_cpu_ticks(pid: int) -> int:
    """utime+stime for the whole process, in clock ticks."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    return int(fields[11]) + int(fields[12])  # utime, stime (0-indexed after state)


def _thread_cpu_ticks(pid: int) -> dict[str, int]:
    """Per-thread utime+stime, keyed by ``tid (name)``.

    The half that matters. A process at 0.8 cores looks fine; the same process with one thread
    at 0.75 is a spinning inference pool, and only the per-thread view says which."""
    out: dict[str, int] = {}
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            raw = (task / "stat").read_text()
        except OSError:  # a thread that exited between listing and reading
            continue
        head, _, rest = raw.partition(" (")
        name, _, tail = rest.rpartition(") ")
        fields = tail.split()
        out[f"{head}:{name}"] = int(fields[11]) + int(fields[12])
    return out


def _temp_c() -> float | None:
    try:
        raw = subprocess.run(
            ["vcgencmd", "measure_temp"], capture_output=True, text=True, timeout=5
        ).stdout
        return float(raw.split("=")[1].split("'")[0])
    except Exception:
        try:  # a generic thermal zone, so this still reports on a non-Pi Linux box
            return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
        except Exception:
            return None


def _throttled() -> str | None:
    try:
        return subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except Exception:
        return None


# ── the run ──────────────────────────────────────────────────────────────────────────────────


class _EventLog:
    """Every ``vision.*`` fact the run published, with the monotonic second it landed."""

    def __init__(self) -> None:
        self.rows: list[tuple[float, str]] = []
        self._t0 = time.monotonic()

    async def handle(self, event: Event) -> None:
        self.rows.append((round(time.monotonic() - self._t0, 2), type(event).__name__))

    def count(self, name: str) -> int:
        return sum(1 for _, kind in self.rows if kind == name)


async def _measure(args: argparse.Namespace, config: Config) -> dict[str, Any]:
    """Run the real service against the real devices and observe. Publishes nothing."""
    import os
    from concurrent.futures import ThreadPoolExecutor

    from avid.adapters.clock import SystemClock
    from avid.core.state_manager import StateManager
    from avid.domain import RobotState
    from avid.domain.vision import PresenceParams
    from avid.main import _build_camera, _build_face_detector
    from avid.services.presence import PresenceService

    pid = os.getpid()

    clock = SystemClock()
    bus = AsyncioEventBus(clock=clock)
    log = _EventLog()
    for event_type in (VisionPresenceGained, VisionPresenceLost, VisionFaceDetected):
        bus.subscribe(event_type, log.handle, name=f"gate.{event_type.__name__}")

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vision")
    service = PresenceService(
        bus=bus,
        clock=clock,
        state=StateManager(bus=bus, clock=clock, initial=RobotState.IDLE),
        camera=_build_camera(config),
        detector=_build_face_detector(config, executor=pool),
        executor=pool,
        fps=config.vision.fps,
        params=PresenceParams(
            confidence_threshold=config.vision.confidence_threshold,
            gain_window_s=config.vision.gain_window_s,
            lose_window_s=config.vision.lose_window_s,
        ),
        nap_after_s=config.vision.nap_after_s,
    )

    temps: list[tuple[float, float]] = []
    seconds = int(args.minutes * 60)

    async with bus:
        await service.start()
        # Let the one-time ONNX session build finish before the CPU baseline is taken. It is a
        # real ~1.3 s of work (measured, #225) and it is not the steady state this grades —
        # billing it to the sustained average would overstate the cost of every short run and
        # understate it for a long one. The P8 gate carves out the same thing (#130).
        await asyncio.sleep(3.0)

        cpu0, threads0, wall0 = (
            _proc_cpu_ticks(pid),
            _thread_cpu_ticks(pid),
            time.monotonic(),
        )
        frames0 = service.frames

        print(f"measuring for {args.minutes:.0f} min (after a 3 s warm-up)...")
        for elapsed in range(seconds):
            await asyncio.sleep(1.0)
            temp = _temp_c()
            if temp is not None:
                temps.append((float(elapsed), temp))
            if elapsed and elapsed % 60 == 0:
                busy = (
                    (_proc_cpu_ticks(pid) - cpu0)
                    / _CLOCK_TICKS
                    / (time.monotonic() - wall0)
                )
                print(
                    f"  {elapsed // 60:>3} min  {busy:.2f} cores  "
                    f"{service.frames - frames0} frames  "
                    f"{temp:.1f}C  {len(log.rows)} vision events"
                )

        wall = time.monotonic() - wall0
        cpu = (_proc_cpu_ticks(pid) - cpu0) / _CLOCK_TICKS
        threads = {
            name: (ticks - threads0.get(name, 0)) / _CLOCK_TICKS / wall
            for name, ticks in _thread_cpu_ticks(pid).items()
        }
        frames = service.frames - frames0
        failures = service.failures
        await service.stop()

    return {
        "wall_s": round(wall, 1),
        "cores": round(cpu / wall, 3),
        "threads": {
            k: round(v, 3) for k, v in sorted(threads.items(), key=lambda kv: -kv[1])
        },
        "frames": frames,
        "failures": failures,
        "fps": round(frames / wall, 2),
        "temps": temps,
        "throttled": _throttled(),
        "events": log.rows,
        "gained": log.count("VisionPresenceGained"),
        "lost": log.count("VisionPresenceLost"),
        "face_detected": log.count("VisionFaceDetected"),
    }


# ── grading ──────────────────────────────────────────────────────────────────────────────────


def _grade(result: dict[str, Any], config: Config) -> list[_Criterion]:
    out: list[_Criterion] = []

    cores = result["cores"]
    hottest = list(result["threads"].items())[:4]
    # ⚠️ Whether anyone was actually in frame changes what this number means. An empty room is
    # the CHEAP case: the detector still runs on every frame, but NMS and the box decode have
    # almost nothing to do. So a pass measured against an empty room is a **floor**, and
    # reporting it as though it were the occupied-room cost would be exactly the kind of
    # quietly-wrong summary line §7.1 is about. The harness cannot know whether a person was
    # there — but it knows whether the detector ever saw a face, which is the honest proxy.
    saw_faces = result["face_detected"] > 0 or result["gained"] > 0
    occupancy = (
        "measured with faces in frame"
        if saw_faces
        else "⚠️ measured on a room the detector never saw a face in — this is a FLOOR, not "
        "the occupied-room cost. Re-measure with someone present before sealing AC-1."
    )
    out.append(
        _Criterion(
            "AC-1",
            f"CPU: {cores:.2f} cores sustained over {result['wall_s']:.0f}s "
            f"(budget ≤{_CORE_BUDGET:.0f})",
            "pass"
            if cores <= _CORE_BUDGET and saw_faces
            else "recorded"
            if cores <= _CORE_BUDGET
            else "fail",
            f"{occupancy}\n        per-thread, busiest first — a spinning ONNX pool or a "
            f"second executor shows here even when the process total does not",
            [f"{name}: {busy:.3f} cores" for name, busy in hottest],
        )
    )

    # ⚠️ Grade the quantity that is reported. The budget is "≤5 fps", so exceeding the
    # configured rate is the failure; falling short is a *finding*, recorded with the number,
    # never a bar quietly moved to meet it (#226 AC-2, CLAUDE.md §7.1).
    target = float(config.vision.fps)
    achieved = result["fps"]
    if achieved > target * 1.05:
        verdict: _Verdict = "fail"
        detail = f"the loop is free-running above its configured {target:.0f} fps"
    elif achieved < target * 0.9:
        verdict = "fail"
        detail = (
            f"the loop achieved {achieved:.2f} of a configured {target:.0f} fps — detection "
            f"cannot keep up. Record this as a finding; do not lower the configured rate to "
            f"make the number agree with itself."
        )
    else:
        verdict = "pass"
        detail = f"holding its configured rate ({result['frames']} frames, {result['failures']} failed)"
    out.append(
        _Criterion(
            "AC-2",
            f"rate: {achieved:.2f} fps against a configured {target:.0f}",
            verdict,
            detail,
        )
    )

    temps = [t for _, t in result["temps"]]
    throttled = result["throttled"]
    if not temps:
        out.append(
            _Criterion(
                "AC-3",
                "thermals: no temperature source on this host",
                "manual",
                "vcgencmd and thermal_zone0 both unavailable — read the curve by hand",
            )
        )
    else:
        # get_throttled is a bitmask; 0x0 is the only clean value. Anything else has thermal
        # or under-voltage history, and the run cannot be graded as thermally stable.
        clean = throttled is None or throttled.endswith("=0x0")
        rise = temps[-1] - temps[0]
        out.append(
            _Criterion(
                "AC-3",
                f"thermals: {temps[0]:.1f}C → {temps[-1]:.1f}C (peak {max(temps):.1f}C), "
                f"{throttled or 'get_throttled unavailable'}",
                "pass" if clean and max(temps) < _TEMP_WATCH_C else "fail",
                f"rose {rise:+.1f}C over the run"
                + (
                    ""
                    if clean
                    else " — THROTTLED: a Pi that throttles during vision degrades "
                    "the audio path too, which is why this criterion exists"
                ),
            )
        )

    total = result["gained"] + result["lost"]
    out.append(
        _Criterion(
            "AC-4",
            f"events: {total} presence decisions over {result['wall_s'] / 60:.0f} min "
            f"({result['gained']} gained, {result['lost']} lost, "
            f"{result['face_detected']} face_detected)",
            "recorded",
            "compare against the real arrivals and departures during the run — this harness "
            "cannot know them, and a count alone is not the criterion (#225 AC-4)",
            [f"t={t:>7.2f}s  {kind}" for t, kind in result["events"][:20]],
        )
    )

    for ac, what in (
        (
            "AC-5",
            "the robot naps, then WAKES when a person sits down — by log and by eye",
        ),
        ("AC-6", "leaving and returning inside the nap window does NOT nap the robot"),
        (
            "AC-7",
            "a full conversation with vision live: no glitching, no dropped turns",
        ),
        ("AC-8", "a check under materially different light from #225's trace"),
    ):
        out.append(
            _Criterion(
                ac,
                what,
                "manual",
                "needs a human in the room. A harness that scored this from an empty one would "
                "be M4's lesson repeating: a gate that can pass on silence is not a gate.",
            )
        )
    return out


def _report(criteria: list[_Criterion], result: dict[str, Any]) -> int:
    print("\n" + "=" * 78)
    print("M8 gate — #226, measured on the Pi")
    print("=" * 78)
    marks = {"pass": "PASS", "fail": "FAIL", "recorded": "····", "manual": "HUMAN"}
    for c in criteria:
        print(f"[{marks[c.verdict]}] {c.ac}  {c.name}")
        if c.detail:
            print(f"        {c.detail}")
        for row in c.rows:
            print(f"          - {row}")

    passed = sum(1 for c in criteria if c.verdict == "pass")
    failed = sum(1 for c in criteria if c.verdict == "fail")
    manual = sum(1 for c in criteria if c.verdict == "manual")
    recorded = sum(1 for c in criteria if c.verdict == "recorded")
    print("-" * 78)
    print(f"{passed}/{passed + failed} automatable criteria passed; {failed} failed.")
    if recorded:
        print(
            f"{recorded} criterion(s) RECORDED but not graded — the run measured them and "
            f"could not\njudge them (an empty room, or a count with no ground truth to compare "
            f"against)."
        )
    print(
        f"{manual} criterion(s) need a human in the room and are NOT graded here. The gate is "
        f"not sealed\nuntil they are recorded on #226 — by eye and by ear, as M3's face gate "
        f"and M4's audio gate were."
    )
    if failed:
        print(
            "\nA failure here is a result, not an accident. File the defect and fix it, or seal "
            "with\nthe gap explicitly named — as M5 did with O1's P95 and M7 with its 8/12. Do "
            "not widen a\nbudget to fit a measurement."
        )
    # Manual criteria are not passes. Exiting 0 with four of them unrecorded would let a run
    # that measured CPU read as a sealed milestone.
    return 1 if (failed or manual) else 0


async def _main(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print(f"config          {args.config}")
    print(
        f"  adapters      camera={config.adapters.camera} "
        f"face_detector={config.adapters.face_detector}"
    )
    print(
        f"  vision.fps    {config.vision.fps}   detector_scale {config.vision.detector_scale}"
    )
    print(f"  threshold     {config.vision.confidence_threshold}")
    print(
        f"  windows       gain {config.vision.gain_window_s}s / "
        f"lose {config.vision.lose_window_s}s / nap {config.vision.nap_after_s}s"
    )
    # AC-0. The machine is not the repo: a key missing from /etc/robot/config.toml falls back to
    # a schema default *silently*, so a stale config would run this entire gate against the fake
    # detector and pass almost everything.
    if (
        config.adapters.face_detector != "yunet"
        or config.adapters.camera != "picamera2"
    ):
        print(
            "\nREFUSING: [adapters] must be camera=picamera2 and face_detector=yunet. "
            "FakeCamera + FakeFaceDetector compose into a convincing simulator that consumes no "
            "CPU and never mis-detects — measuring THAT and calling it a ≤1-core result is the "
            "exact failure #226 AC-0 exists to prevent.",
            file=sys.stderr,
        )
        return 2

    result = await _measure(args, config)
    criteria = _grade(result, config)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "measured": result,
                    "criteria": [
                        {
                            "ac": c.ac,
                            "name": c.name,
                            "verdict": c.verdict,
                            "detail": c.detail,
                        }
                        for c in criteria
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nevidence -> {args.json}")
    return _report(criteria, result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("/etc/robot/config.toml"))
    parser.add_argument(
        "--minutes", type=float, default=20.0, help="sustained measurement window"
    )
    parser.add_argument("--json", type=Path, help="write the evidence here")
    args = parser.parse_args()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
