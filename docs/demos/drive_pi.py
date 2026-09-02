"""On-Pi M12 gate harness (#400, ADR-015) — drive the real wheels and report what they did.

M12's criteria are **physical**: a step that reads as alive, an edge that stops it, a rail that
does not sag. ``tests/e2e/test_drive_gate.py`` proves the arc composes, but every assertion in it
is made against ``FakeDrive`` and **an odometer is not a moved robot**. A sensor on the display
overlay's pin, a polarity that reads the desk as an edge, a loose 5 V connector, a flip set the
wrong way — every one of those produces a perfect trace and a robot that sits still, or one
that walks off the far side. That is the M4 lesson wearing wheels.

So this runs the **real adapters from the real config** and only *observes*: it drives steps
through the same service the robot uses, reads the same ``drive.*`` facts an operator can see,
and grades what it can measure. What needs an eye or a hand it reports as ``HUMAN`` and
**refuses to call sealed** — the exit code is non-zero while any of those is unrecorded.

⚠️ **Run this with the robot on the desk it will live on, a hand ready, and the rear of the
robot AWAY from any edge.** The first step is ``STEP_TOWARD``; if the flip is wrong the robot
moves *backward* first, and F-14 (SDS §12.1) says the return leg is protected by the budget
alone. Stand behind it.

What it measures:

* **AC-0 (setup)** — the *loaded* config selects the real ``drive`` AND the real ``edge``
  (never one without the other), the pins are not 24/25, and the geometry fits the budget.
  ⚠️ The machine is not the repo: a stale ``/etc/robot/config.toml`` falls back to schema
  defaults, and the defaults ship the FAKE wheels — a fake run here grades an odometer.
* **AC-1** — a ``STEP_TOWARD`` completes: ``drive.step_completed`` with ``net_mm == 0``, and the
  service's odometer back at origin. Whether it *moved the right way* is the hand's job (HUMAN).
* **AC-2** — with ``--edge-test``, a step is started and the operator is asked to put a hand
  under a front sensor: ``drive.step_aborted(reason="edge")`` must appear, the motors must have
  been stopped through the port, and the odometer must read origin again (the retreat).
* **AC-5** — ``vcgencmd get_throttled`` and the kernel's undervoltage log across the run. R-04's
  failure mode is SD-card corruption, so this is sampled throughout, not once at the end.

What it deliberately does not measure: whether the step reads as alive rather than as a
mechanism (AC-7 of #400), whether the robot actually moved the direction the odometer claims,
and whether the sensors see a *real* desk edge rather than a hand. Those need a person.

Usage on the Pi, with ``robot.service`` **stopped** (``deploy/PI_OPERATIONS.md`` §1)::

    /opt/avid/.venv/bin/python docs/demos/drive_pi.py --config /etc/robot/config.toml \\
        --json docs/demos/m12_evidence/gate.json
    /opt/avid/.venv/bin/python docs/demos/drive_pi.py --config /etc/robot/config.toml --edge-test
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
from avid.domain import (  # noqa: E402
    EDGE,
    DriveStepAborted,
    DriveStepCompleted,
    DriveStepStarted,
    Event,
    Gesture,
    RobotState,
    StateTransitioned,
    StepGeometry,
    Trigger,
)
from avid.main import _build_drive, _build_edge_sensor  # noqa: E402
from avid.services.drive import DriveService  # noqa: E402

_ASCII = {
    "—": "--",
    "–": "-",
    "→": "->",
    "≥": ">=",
    "≤": "<=",
    "§": "S",
    "⚠️": "!!",
    "⚠": "!!",
    "·": ".",
}


def _say(text: str = "") -> None:
    """Print *text*, folded to something every console can render (a cp1252 stdout kills a
    gate that prints an em dash, and a gate that crashes while reporting is worse than one that
    reports plainly)."""
    for source, plain in _ASCII.items():
        text = text.replace(source, plain)
    print(text.encode("ascii", "replace").decode("ascii"), flush=True)


_Verdict = Literal["pass", "fail", "recorded", "manual"]


@dataclass
class _Criterion:
    """One reported line. Held rather than printed so **every criterion reports before any
    verdict is decided** (CLAUDE.md §7.1)."""

    ac: str
    name: str
    verdict: _Verdict
    detail: str = ""
    rows: list[str] = field(default_factory=list)


def _throttled() -> str | None:
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value.split("=", 1)[1] if "=" in value else value or None


def _undervoltage_lines() -> list[str]:
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


class _Tap:
    """Records the ``drive.*`` facts — the observable surface an operator can see (§9.1.3)."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def handle(self, event: Event) -> None:
        self.events.append(event)

    def of(self, event_type: type[Event]) -> list[Any]:
        return [e for e in self.events if isinstance(e, event_type)]


def _idle(bus: AsyncioEventBus) -> StateTransitioned:
    """The fact the state machine would publish on reaching IDLE. The harness publishes it
    because the state machine is not under test here — the wheels are."""
    return StateTransitioned(
        event_id=uuid4(),
        correlation_id=uuid4(),
        timestamp_ms=int(time.time() * 1000),
        monotonic_ns=time.monotonic_ns(),
        source="drive_pi",
        from_=RobotState.BOOTING,
        to=RobotState.IDLE,
        trigger=Trigger.SYSTEM_STARTED,
    )


async def _measure(config: Config, args: argparse.Namespace) -> dict[str, Any]:
    """Drive the real wheels through a step (and, on request, an edge abort) and record it.

    ⚠️ Every number below comes from the **run**, never from config or intent (CLAUDE.md §7.1):
    the pins, the flip and the geometry are read back off the objects that were actually built.
    """
    drive = _build_drive(config)
    edge = _build_edge_sensor(config)
    bus = AsyncioEventBus()
    tap = _Tap()
    service = DriveService(
        bus=bus,
        drive=drive,
        edge=edge,
        clock=SystemClock(),
        geometry=StepGeometry(
            step_mm=config.drive.step_mm,
            max_excursion_mm=config.drive.max_excursion_mm,
            speed_frac=config.drive.speed_frac,
            dwell_ms=config.drive.dwell_ms,
        ),
        edge_poll_ms=config.drive.edge.poll_ms,
        edge_clear_hold_ms=config.drive.edge.clear_hold_ms,
        # The idle scheduler is pushed out of the way, not switched off: this gate drives steps.
        idle_step_interval_min_s=3600.0,
        idle_step_interval_max_s=7200.0,
    )
    for sub in service.subscriptions():
        bus.subscribe(
            sub.event_type,
            sub.handler,
            name=sub.name,
            policy=sub.policy,
            maxsize=sub.maxsize,
        )
    for event_type in (DriveStepStarted, DriveStepCompleted, DriveStepAborted):
        bus.subscribe(event_type, tap.handle, name=f"gate.{event_type.__name__}")

    result: dict[str, Any] = {
        "adapters": {"drive": config.adapters.drive, "edge": config.adapters.edge},
        "pins": {
            "left": [config.drive.left_forward_pin, config.drive.left_backward_pin],
            "right": [config.drive.right_forward_pin, config.drive.right_backward_pin],
            "edge": list(config.drive.edge.pins),
        },
        "forward_is_inverted": config.drive.forward_is_inverted,
        "active_high": config.drive.edge.active_high,
        "geometry": {
            "step_mm": config.drive.step_mm,
            "max_excursion_mm": config.drive.max_excursion_mm,
            "speed_frac": config.drive.speed_frac,
            "mm_per_s_at_full": config.drive.mm_per_s_at_full,
        },
        "throttled_before": _throttled(),
        "edge_test": bool(args.edge_test),
    }

    await bus.start()
    await service.start()
    await bus.publish(_idle(bus))
    for _ in range(6):
        await asyncio.sleep(0)
    started = time.monotonic()
    try:
        _say(
            "  sensors read clear? "
            + (
                "yes"
                if await edge.clear()
                else "NO -- an edge, or the polarity is wrong"
            )
        )
        result["clear_before"] = await edge.clear()

        # --- AC-1: one step, out and back ------------------------------------------------
        _say("  performing step_toward ... (stand BEHIND the robot)")
        await service.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
        await _await_step(service)
        await asyncio.sleep(args.settle_s)
        result["offset_after_step_mm"] = service.offset_mm

        # --- AC-2: the hand ----------------------------------------------------------------
        if args.edge_test:
            _say(
                "  starting a SLOW step_toward -- put a hand under a FRONT sensor while it moves"
            )
            slow = DriveService(
                bus=bus,
                drive=drive,
                edge=edge,
                clock=SystemClock(),
                geometry=StepGeometry(
                    step_mm=config.drive.max_excursion_mm,
                    max_excursion_mm=config.drive.max_excursion_mm,
                    speed_frac=max(0.2, config.drive.speed_frac / 2),
                    dwell_ms=0,
                ),
                edge_poll_ms=config.drive.edge.poll_ms,
                edge_clear_hold_ms=config.drive.edge.clear_hold_ms,
                idle_step_interval_min_s=3600.0,
                idle_step_interval_max_s=7200.0,
            )
            # The slow service reads the same feed; it was not registered, so tell it directly.
            await slow._on_state_transitioned(_idle(bus))  # noqa: SLF001 - harness, not app
            await slow.perform(Gesture.STEP_TOWARD, correlation_id=uuid4())
            await _await_step(slow)
            await asyncio.sleep(args.settle_s)
            result["offset_after_edge_mm"] = slow.offset_mm
            result["edge_latched"] = slow.edge_latched
            await slow.stop()
    finally:
        await service.stop()
        await bus.stop()

    result["wall_s"] = time.monotonic() - started
    result["throttled_after"] = _throttled()
    result["undervoltage"] = _undervoltage_lines()
    result["started"] = [
        {"gesture": e.gesture, "heading": e.heading, "distance_mm": e.distance_mm}
        for e in tap.of(DriveStepStarted)
    ]
    result["completed"] = [
        {"gesture": e.gesture, "duration_ms": e.duration_ms, "net_mm": e.net_mm}
        for e in tap.of(DriveStepCompleted)
    ]
    result["aborted"] = [
        {"gesture": e.gesture, "reason": e.reason} for e in tap.of(DriveStepAborted)
    ]
    return result


async def _await_step(service: DriveService) -> None:
    task = service._task  # noqa: SLF001 - the harness observes; it does not reimplement
    if task is None:
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), 30.0)
    except (TimeoutError, asyncio.CancelledError):
        return


def _grade(result: dict[str, Any], config: Config) -> list[_Criterion]:
    out: list[_Criterion] = []
    adapters = result["adapters"]
    pins = result["pins"]

    real = adapters["drive"] == "l9110s" and adapters["edge"] == "tcrt5000"
    half_real = (adapters["drive"] == "l9110s") != (adapters["edge"] == "tcrt5000")
    bad_pins = {24, 25} & set(pins["edge"])
    out.append(
        _Criterion(
            "AC-0",
            f"loaded config: drive={adapters['drive']!r}, edge={adapters['edge']!r}, "
            f"edge pins {pins['edge']}, flip={result['forward_is_inverted']}",
            "pass" if (real and not bad_pins) else "fail",
            "⚠️ the machine is not the repo -- a stale /etc/robot/config.toml falls back to "
            "SCHEMA DEFAULTS,\n        which ship the FAKE wheels; and real wheels with a fake "
            "sensor is a robot that steps blind"
            + (
                "\n        !! HALF-REAL: one adapter real, the other fake"
                if half_real
                else ""
            )
            + (
                "\n        !! GPIO 24/25 belong to the display overlay"
                if bad_pins
                else ""
            ),
        )
    )

    completed = result["completed"]
    net_zero = bool(completed) and all(c["net_mm"] == 0.0 for c in completed)
    home = abs(result.get("offset_after_step_mm", 1e9)) < 1.0
    out.append(
        _Criterion(
            "AC-1",
            f"step_toward: {len(completed)} completed, "
            f"odometer {result.get('offset_after_step_mm', float('nan')):.1f} mm from origin",
            "pass" if (net_zero and home) else "fail",
            "the plan's net_mm and the service's odometer both say origin -- whether the robot "
            "went\n        the RIGHT WAY first is the hand's job below",
            [
                f"{c['gesture']}: {c['duration_ms']} ms, net {c['net_mm']} mm"
                for c in completed
            ],
        )
    )

    if result["edge_test"]:
        edge_aborts = [a for a in result["aborted"] if a["reason"] == EDGE]
        retreated = abs(result.get("offset_after_edge_mm", 1e9)) < 2.0
        out.append(
            _Criterion(
                "AC-2",
                f"hand under the sensor: {len(edge_aborts)} edge abort(s), odometer "
                f"{result.get('offset_after_edge_mm', float('nan')):.1f} mm, "
                f"latched={result.get('edge_latched')}",
                "pass" if (edge_aborts and retreated) else "fail",
                "drive.step_aborted(reason=edge) is what an operator can see; the odometer back "
                "at origin\n        is the retreat. No abort means the sensor never saw the hand "
                "-- pin, polarity, or wiring",
            )
        )
    else:
        out.append(
            _Criterion(
                "AC-2",
                "edge abort -- NOT RUN (pass --edge-test and have a hand ready)",
                "manual",
            )
        )

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
            "R-04 with FOUR actuators on the rail now. `None` means vcgencmd was unavailable: "
            "recorded,\n        not passed. A meter on the rail is still owed (SPK-6 / #206)",
            result["undervoltage"][:5],
        )
    )

    for ac, what in (
        (
            "AC-1h",
            "the robot moved TOWARD the user first, then back -- if it went backward first, "
            "[drive] forward_is_inverted is wrong: fix the config, not the code",
        ),
        (
            "AC-7",
            "a step reads as alive rather than as a mechanism (the micro_motion bar) -- watch "
            "three idle steps with the service running",
        ),
        (
            "SPK-6",
            "put a ruler to it: how many mm did a full-duty second travel? Pin [drive] "
            "mm_per_s_at_full and delete PROVISIONAL",
        ),
    ):
        out.append(_Criterion(ac, what, "manual"))
    return out


def _write_evidence(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    _say(f"\nevidence written to {path}")


def _report(criteria: list[_Criterion]) -> int:
    _say("\n" + "=" * 78)
    _say("M12 gate -- #400, measured on the Pi")
    _say("=" * 78)
    marks = {"pass": "PASS", "fail": "FAIL", "recorded": "....", "manual": "HUMAN"}
    for c in criteria:
        _say(f"[{marks[c.verdict]}] {c.ac}  {c.name}")
        if c.detail:
            _say(f"        {c.detail}")
        for row in c.rows:
            _say(f"          - {row}")
    passed = sum(1 for c in criteria if c.verdict == "pass")
    failed = sum(1 for c in criteria if c.verdict == "fail")
    manual = sum(1 for c in criteria if c.verdict == "manual")
    _say("-" * 78)
    _say(f"{passed}/{passed + failed} automatable criteria passed; {failed} failed.")
    _say(
        f"{manual} criterion(s) need a person and are NOT graded here. An odometer is not a "
        f"moved robot:\na wrong flip walks off the far edge with a perfect trace. Not sealed "
        f"until these are recorded on #400."
    )
    return 1 if (failed or manual) else 0


async def _main(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _say(f"config          {args.config}")
    _say(f"  adapters      drive={config.adapters.drive} edge={config.adapters.edge}")
    _say(
        f"  [drive]       step {config.drive.step_mm} mm of {config.drive.max_excursion_mm} mm "
        f"budget at {config.drive.speed_frac:.0%}, flip={config.drive.forward_is_inverted}"
    )
    if config.adapters.drive != "l9110s" or config.adapters.edge != "tcrt5000":
        _say(
            "\n!!  [adapters] drive/edge are NOT both real. This run grades an odometer, not a "
            "robot.\n    Flip them TOGETHER in /etc/robot/config.toml before sealing anything.\n"
        )
    result = await _measure(config, args)
    criteria = _grade(result, config)
    if args.json:
        _write_evidence(Path(args.json), result)
    return _report(criteria)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", required=True, help="path to the LOADED config TOML"
    )
    parser.add_argument("--json", help="write the raw measurements here")
    parser.add_argument("--settle-s", type=float, default=1.0)
    parser.add_argument(
        "--edge-test",
        action="store_true",
        help="run a slow step and ask for a hand under a front sensor (AC-2)",
    )
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
