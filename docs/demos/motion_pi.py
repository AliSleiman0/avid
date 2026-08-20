"""On-Pi M9 gate harness (#207) — drive the real rig and report what it actually did.

M9's criteria are mostly **physical**, and physical criteria are exactly what a green suite
cannot see. ``tests/e2e/test_m9_gate.py`` proves the arc composes; every assertion in it is
made against ``FakeServo``, and **a trace is not a moved head**. A wrong channel, a stale
one-axis config on the machine, a horn slipping on its spline, an I²C address collision — every
one of those produces a perfect trace and a motionless robot. That is the M4 lesson (the gate
printed ``PASS`` over a mute robot) wearing a servo.

So this runs the **real composition root against the real config** and only *observes*: it
publishes no events, injects no frames, and drives motion through the same surfaces the robot
uses. What it can measure, it grades. What needs an eye or an ear, it reports as ``HUMAN`` and
**refuses to call sealed** — the exit code is non-zero while any of those is unrecorded, because
a run that measured thermals and called the milestone done would be M4 repeating.

What it measures:

* **AC-0 (setup)** — that the *loaded* config declares both axes, that ``[adapters] servo`` is
  the real one, and which channels they land on. ⚠️ The machine is not the repo: a stale
  ``/etc/robot/config.toml`` falls back to **schema defaults** rather than erroring, so a Pi
  still carrying the one-axis ``[servo]`` would run this entire gate with no tilt and report
  success on everything that does not need it.
* **AC-2** — nod and turn, on *different* axes. The claim ADR-009's second servo makes, and one
  that was unsatisfiable on the old rig (§2.7.1: "nod **or** turn, not both").
* **AC-3** — preemption, from the log: ``motion.gesture_preempted`` with the interrupter in
  ``by``, and a *partial* trace for the gesture that was cut.
* **AC-4** — that every channel is de-energised after ``[motion] idle_relax_ms``. ⚠️ This
  measures the **command**, not the silence. Whether the rig actually stops humming is an ear's
  job and is reported HUMAN.
* **AC-5** — ``vcgencmd get_throttled`` and the kernel's undervoltage log across the whole run.
  R-04's failure mode is not a glitch, it is **SD-card corruption**, so this is sampled
  throughout rather than once at the end.

What it deliberately does not measure, and says so rather than implying otherwise: whether the
nod reads as a nod (AC-1), whether the rig is audibly silent (AC-4), whether a spoken "look
left" moves the body (AC-6), and whether the drift reads as alive rather than twitchy (AC-7).
Those need a person, and a harness that scored them from an empty room would be the M4 lesson
repeating.

Usage on the Pi, with ``robot.service`` **stopped** (``deploy/PI_OPERATIONS.md`` §1)::

    /opt/avid/.venv/bin/python docs/demos/motion_pi.py --config /etc/robot/config.toml \\
        --json docs/demos/m9_evidence/gate.json
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
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from avid.adapters.clock import SystemClock  # noqa: E402
from avid.core.config import Config, load_config  # noqa: E402
from avid.core.event_bus import AsyncioEventBus  # noqa: E402
from avid.core.hal import Axis  # noqa: E402
from avid.domain import (  # noqa: E402
    Event,
    Gesture,
    MotionGestureCompleted,
    MotionGesturePreempted,
    MotionGestureStarted,
    plan,
)
from avid.main import _build_servo  # noqa: E402
from avid.services.motion import MotionService  # noqa: E402

# The report is read on whatever console is to hand -- an SSH session, a Windows terminal, a CI
# log -- and `print` raises UnicodeEncodeError on a cp1252 stdout. A gate tool that CRASHES
# while reporting is worse than one that reports plainly, so every line goes through here and
# the typography degrades instead of the run. (Found by running this on the dev box: the very
# first warning it tried to print killed it.)
_ASCII = {
    "—": "--",
    "–": "-",
    "→": "->",
    "°": " deg",
    "≥": ">=",
    "≤": "<=",
    "§": "S",
    "²": "2",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "⚠️": "!!",
    "⚠": "!!",
    "·": ".",
}


def _say(text: str = "") -> None:
    """Print *text*, folded to something every console can render."""
    for source, plain in _ASCII.items():
        text = text.replace(source, plain)
    print(text.encode("ascii", "replace").decode("ascii"))


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


# ── the Pi's own view of its power ───────────────────────────────────────────────────────────


def _throttled() -> str | None:
    """``vcgencmd get_throttled``, or ``None`` where the tool does not exist.

    ⚠️ Read from the **Pi**, not from a meter. R-04's failure mode is a brown-out mid-write, and
    the SoC detects undervoltage events a slow multimeter reading misses entirely — including
    ones that have already been and gone, which is the bit that matters for a card.
    """
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value.split("=", 1)[1] if "=" in value else value or None


def _undervoltage_lines() -> list[str]:
    """Kernel undervoltage warnings, if the log is readable.

    The second instrument, because the two disagree in a useful way: ``get_throttled``'s sticky
    bits say *"this happened at some point"*, while a dmesg line has a timestamp that can be
    lined up against the moment a servo stalled.
    """
    try:
        out = subprocess.run(
            ["dmesg", "--notime"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [
        line.strip()
        for line in out.stdout.splitlines()
        if "voltage" in line.lower() or "throttl" in line.lower()
    ]


# ── the run ──────────────────────────────────────────────────────────────────────────────────


class _Tap:
    """Records the ``motion.*`` facts, which is how AC-3 is read.

    The events, not the servo's internal trace: §9.1.3 makes them the observable surface, and
    #207 grades preemption *"by log and by eye"*. Reading the adapter's private bookkeeping
    instead would grade something no operator can see on the night.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)

    def of(self, event_type: type[Event]) -> list[Any]:
        return [e for e in self.events if isinstance(e, event_type)]


async def _measure(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    """Drive the real rig through the arc and record what happened.

    ⚠️ Every number below comes from the **run**, never from config or intent (CLAUDE.md §7.1).
    The reach limits, the channel numbers and the relax window are read back off the objects
    that were actually built, so a banner cannot quote a value the run did not use.
    """
    servo = _build_servo(config)
    bus = AsyncioEventBus()
    tap = _Tap()
    motion = MotionService(
        bus=bus,
        servo=servo,
        clock=SystemClock(),
        idle_relax_ms=config.motion.idle_relax_ms,
        look_at_cooldown_ms=config.motion.look_at_cooldown_ms,
        drift_interval_min_s=config.motion.micro_motion_interval_min_s,
        drift_interval_max_s=config.motion.micro_motion_interval_max_s,
        drift_amplitude_frac=config.motion.micro_motion_amplitude_frac,
    )
    for event_type in (
        MotionGestureStarted,
        MotionGestureCompleted,
        MotionGesturePreempted,
    ):
        bus.subscribe(event_type, tap.handle, name=f"gate.{event_type.__name__}")

    axes: tuple[Axis, ...] = servo.axes
    result: dict[str, Any] = {
        "axes": [
            {
                "name": a.name,
                "channel": a.channel,
                "min_deg": a.min_deg,
                "max_deg": a.max_deg,
            }
            for a in axes
        ],
        "adapter": config.adapters.servo,
        "idle_relax_ms": config.motion.idle_relax_ms,
        "throttled_before": _throttled(),
    }

    await bus.start()
    await motion.start()
    started = time.monotonic()
    try:
        # --- AC-2: nod and turn, each on its own axis ------------------------------------
        for gesture in (
            Gesture.NOD,
            Gesture.TURN_LEFT,
            Gesture.TURN_RIGHT,
            Gesture.CENTER,
        ):
            _say(f"  performing {gesture.name.lower()} …")
            await motion.perform(gesture, correlation_id=uuid4())
            await _await_gesture(motion)
            await asyncio.sleep(args.settle_s)

        # --- AC-3: preemption, mid-sweep --------------------------------------------------
        _say("  performing nod, then cutting it with a turn …")
        await motion.perform(Gesture.NOD, correlation_id=uuid4())
        await asyncio.sleep(args.preempt_after_s)
        await motion.perform(Gesture.TURN_LEFT, correlation_id=uuid4())
        await _await_gesture(motion)

        # --- AC-4: the relax window -------------------------------------------------------
        window_s = config.motion.idle_relax_ms / 1000
        _say(f"  idling {window_s + args.settle_s:.1f}s for the relax window …")
        await asyncio.sleep(window_s + args.settle_s)
        result["energised_after_idle"] = {
            axis.name: bool(servo.is_energised(axis.channel)) for axis in axes
        }

        await motion.perform(Gesture.CENTER, correlation_id=uuid4())
        await _await_gesture(motion)
    finally:
        await motion.stop()
        await bus.stop()

    result["wall_s"] = time.monotonic() - started
    result["energised_after_stop"] = {
        axis.name: bool(servo.is_energised(axis.channel)) for axis in axes
    }
    result["throttled_after"] = _throttled()
    result["undervoltage"] = _undervoltage_lines()
    result["gestures"] = [
        {"gesture": e.gesture, "axes": [a.name for a in e.axes]}
        for e in tap.of(MotionGestureStarted)
    ]
    result["completed"] = [
        {"gesture": e.gesture, "duration_ms": e.duration_ms}
        for e in tap.of(MotionGestureCompleted)
    ]
    result["preempted"] = [
        {"gesture": e.gesture, "by": e.by} for e in tap.of(MotionGesturePreempted)
    ]
    return result


async def _await_gesture(motion: MotionService) -> None:
    """Wait for the gesture in flight, if any. Reads the service's own handle."""
    task = motion._task  # noqa: SLF001 - the harness observes; it does not reimplement
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), 10.0)
    except (TimeoutError, asyncio.CancelledError):
        return


# ── grading ──────────────────────────────────────────────────────────────────────────────────


def _grade(result: dict[str, Any], config: Config) -> list[_Criterion]:
    out: list[_Criterion] = []
    axes = result["axes"]

    # AC-0 — the setup criterion, and the one most likely to be quietly wrong.
    real_adapter = result["adapter"] == "pca9685"
    two_axes = len(axes) >= 2
    out.append(
        _Criterion(
            "AC-0",
            f"loaded config: {len(axes)} axis/axes, [adapters] servo={result['adapter']!r}",
            "pass" if (real_adapter and two_axes) else "fail",
            "⚠️ the machine is not the repo — a stale /etc/robot/config.toml falls back to "
            "SCHEMA DEFAULTS\n        rather than erroring, so a one-axis rig here means the "
            "whole gate ran without tilt",
            [
                f"{a['name']}: channel {a['channel']}, reach {a['min_deg']}–{a['max_deg']}°"
                for a in axes
            ],
        )
    )

    # AC-2 — nod AND turn, and on different axes. The 2 DoF claim.
    by_gesture = {g["gesture"]: g["axes"] for g in result["gestures"]}
    nod_axes = set(by_gesture.get("nod", []))
    turn_axes = set(by_gesture.get("turn_left", [])) | set(
        by_gesture.get("turn_right", [])
    )
    both = bool(nod_axes and turn_axes and nod_axes != turn_axes)
    out.append(
        _Criterion(
            "AC-2",
            f"nod on {sorted(nod_axes) or '—'}, turn on {sorted(turn_axes) or '—'}",
            "pass" if both else "fail",
            "different axes is the whole claim: on a 1-servo rig a nod degrades to a pan sway, "
            "so\n        'it moved' passes on the hardware ADR-009 replaced",
        )
    )

    # AC-3 — preemption, read from the log rather than from the adapter.
    preempted = result["preempted"]
    superseded = [p for p in preempted if p["by"] is not None]
    faults = [p for p in preempted if p["by"] is None]
    out.append(
        _Criterion(
            "AC-3",
            f"{len(superseded)} gesture(s) preempted by a newer one",
            "pass" if superseded else "fail",
            "read from motion.gesture_preempted, which is what an operator can see — and what "
            "AC-3\n        asks to be reconciled against the head by eye",
            [f"{p['gesture']} cut by {p['by']}" for p in superseded],
        )
    )
    if faults:
        out.append(
            _Criterion(
                "AC-3b",
                f"⚠️ {len(faults)} gesture(s) aborted by a DEVICE FAULT (by=None)",
                "fail",
                "§3.12.3's I²C-fault path fired. The service recovered by design, but a fault "
                "during a\n        gate run is a finding: check the rail, the address and the "
                "wiring before sealing",
                [f"{p['gesture']} aborted" for p in faults],
            )
        )

    # AC-4 — the relax COMMAND. The silence itself is an ear's job.
    idle = result.get("energised_after_idle", {})
    still_held = [name for name, energised in idle.items() if energised]
    out.append(
        _Criterion(
            "AC-4",
            f"after {config.motion.idle_relax_ms} ms idle: "
            f"{'all channels de-energised' if not still_held else f'STILL HELD: {still_held}'}",
            "pass" if idle and not still_held else "fail",
            "⚠️ this measures the COMMAND, not the silence — whether the rig actually stops "
            "humming\n        is AC-4's other half and needs an ear in a quiet room",
        )
    )

    held_after_stop = [
        name for name, energised in result["energised_after_stop"].items() if energised
    ]
    out.append(
        _Criterion(
            "AC-4b",
            f"after shutdown: {'all channels released' if not held_after_stop else f'STILL HELD: {held_after_stop}'}",
            "pass" if not held_after_stop else "fail",
            "the one failure that outlives the process — a held channel keeps drawing current "
            "after\n        the program is gone, until somebody pulls the plug",
        )
    )

    # AC-5 — the Pi's own undervoltage view, sampled across the run.
    before, after = result["throttled_before"], result["throttled_after"]
    clean = after in {"0x0", None} and not result["undervoltage"]
    out.append(
        _Criterion(
            "AC-5",
            f"vcgencmd get_throttled: {before} → {after}",
            "pass"
            if (clean and after is not None)
            else "recorded"
            if after is None
            else "fail",
            "R-04's failure mode is SD-card corruption, not a glitch — so this is the Pi's own "
            "view,\n        not a meter's. `None` means vcgencmd was unavailable: recorded, "
            "not passed",
            result["undervoltage"][:5],
        )
    )

    # Duration sanity: measured against what plan() predicted, on the rig's own axes.
    axis_values = tuple(
        Axis(
            name=a["name"],
            channel=a["channel"],
            min_deg=a["min_deg"],
            max_deg=a["max_deg"],
        )
        for a in axes
    )
    rows = []
    for completed in result["completed"]:
        gesture = Gesture[completed["gesture"].upper()]
        predicted = sum(f.duration_ms for f in plan(gesture, axis_values))
        rows.append(
            f"{completed['gesture']}: {completed['duration_ms']} ms measured, "
            f"{predicted} ms planned"
        )
    out.append(
        _Criterion(
            "AC-8",
            "gesture durations, measured vs planned",
            "recorded",
            "a measured duration well over the planned one is a loop under load — which is "
            "what AC-8\n        is really asking about, and it is not a pass/fail number. "
            "!! Only meaningful on the Pi: off it,\n        the sweep's per-step sleep is "
            "dominated by the host's timer granularity (~1.5x over on a\n        Windows dev "
            "box), which is a fact about the host and not about the robot",
            rows,
        )
    )

    # The four that need a person. Reported, never scored.
    for ac, what in (
        (
            "AC-1",
            "HAPPY produces a nod that READS as a nod, on the tilt axis — record a video",
        ),
        (
            "AC-4",
            "the rig is audibly silent when idle — listen in a quiet room, and feel for warmth",
        ),
        (
            "AC-6",
            'say "look left" and "look up" live; confirm the TOOL CALL appears in the log with its arguments',
        ),
        (
            "AC-7",
            "idle micro-motion reads as alive rather than twitchy, and does not defeat AC-4",
        ),
    ):
        out.append(_Criterion(ac, what, "manual"))

    return out


def _write_evidence(path: Path, result: dict[str, Any]) -> None:
    """Keep the artefact, not only the statistic (CLAUDE.md §7.1).

    The report above is a reading of this file; the file is what a later session can re-read
    when a number in the journal is questioned. M6's dominant defect family was reports that
    described something other than the run, and the cheapest defence is keeping the run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    _say(f"\nevidence written to {path}")


def _report(criteria: list[_Criterion], result: dict[str, Any]) -> int:
    _say("\n" + "=" * 78)
    _say("M9 gate — #207, measured on the Pi")
    _say("=" * 78)
    marks = {"pass": "PASS", "fail": "FAIL", "recorded": "····", "manual": "HUMAN"}
    for c in criteria:
        _say(f"[{marks[c.verdict]}] {c.ac}  {c.name}")
        if c.detail:
            _say(f"        {c.detail}")
        for row in c.rows:
            _say(f"          - {row}")

    passed = sum(1 for c in criteria if c.verdict == "pass")
    failed = sum(1 for c in criteria if c.verdict == "fail")
    manual = sum(1 for c in criteria if c.verdict == "manual")
    recorded = sum(1 for c in criteria if c.verdict == "recorded")
    _say("-" * 78)
    _say(f"{passed}/{passed + failed} automatable criteria passed; {failed} failed.")
    if recorded:
        _say(
            f"{recorded} criterion(s) RECORDED but not graded — the run measured them and has "
            f"no bar to\njudge them against."
        )
    _say(
        f"{manual} criterion(s) need a person and are NOT graded here. ⚠️ FakeServo records a "
        f"trace and a\ntrace is not a moved head — a wrong channel, a stale config or a horn "
        f"slipping on its spline\nall produce a perfect trace and a motionless robot. The gate "
        f"is not sealed until these are\nrecorded on #207, by eye and by ear."
    )
    if failed:
        _say(
            "\nA failure here is a result, not an accident. File the defect and fix it before "
            "tagging —\nthe M5 bench produced three real defects precisely because someone "
            "watched the hardware."
        )
    # Manual criteria are not passes. Exiting 0 with four unrecorded would let a run that
    # measured a relax window read as a sealed milestone.
    return 1 if (failed or manual) else 0


async def _main(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _say(f"config          {args.config}")
    _say(f"  adapters      servo={config.adapters.servo}")
    _say(f"  [motion]      idle_relax_ms={config.motion.idle_relax_ms}")
    if config.adapters.servo != "pca9685":
        _say(
            "\n⚠️  [adapters] servo is NOT 'pca9685'. This run will drive the FAKE servo and "
            "every\n    criterion below will be measured against a trace rather than a rig. "
            "That is the M4\n    failure exactly. Flip it in /etc/robot/config.toml before "
            "sealing anything.\n"
        )

    result = await _measure(config, args)
    criteria = _grade(result, config)
    if args.json:
        _write_evidence(Path(args.json), result)
    return _report(criteria, result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", required=True, help="path to the LOADED config TOML"
    )
    parser.add_argument("--json", help="write the raw measurements here")
    parser.add_argument(
        "--settle-s",
        type=float,
        default=1.0,
        help="pause between gestures, so a watcher can tell them apart",
    )
    parser.add_argument(
        "--preempt-after-s",
        type=float,
        default=0.25,
        help="how far into the nod to cut it — long enough to be visibly mid-sweep",
    )
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
