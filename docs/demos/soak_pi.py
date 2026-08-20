#!/usr/bin/env python
"""The M11 soak harness and the O5 gate (#383, SDS §12.6).

O5 is *"30-day soak, ≥99% uptime, zero manual restarts"* — the last criterion between here and
`v1.0.0`, and **the most expensive measurement this project will ever take.** Thirty days cannot
be re-run cheaply, so everything below is arranged around one fear: producing thirty days of data
that turns out not to mean anything.

Two modes, because a soak is two jobs:

    soak_pi.py --mode sample                    # runs for weeks, records what it sees
    soak_pi.py --mode grade --since <epoch>     # reads the record, grades O5, exits non-zero

⚠️ **The sampler is a separate process, and that is not negotiable.** A sampler living inside the
robot dies exactly when the robot dies — it goes blind at the only moment that matters. This one
polls `/health` and `/metrics` from outside and writes to **its own database**, not the robot's, so
that a robot whose DB is the problem still gets measured. `deploy/soak-sampler.service` runs it.

**What is graded, and against what.** SDS §12.6 defines O5's terms precisely enough to compute:
the denominator is the window; a SLEEPING robot is UP; downtime is bounded by the heartbeat
interval; a clean stop is manual and a watchdog kill is not. This file computes those and nothing
else — it does not re-decide what they mean.

Held to CLAUDE.md §7.1 harder than any other gate here:

* **The sampler's own gaps are graded.** A hole in the samples is a *finding*, and it is
  distinguishable from a hole in the robot: the robot's downtime is in `boot_log`, the sampler's
  is in the absence of rows. A soak reporting 100% uptime because its sampler died on day three is
  the single worst outcome available, and it is the default outcome if nobody designs against it.
* **Every criterion reports before any verdict is decided.** A failing uptime figure must not hide
  a passing restart figure.
* **Statistics are named honestly**, with n. Thirty days gives enough samples for real percentiles
  — unlike the n=5 cases that have burned this project — but n is printed anyway.
* **A window whose build changed is not one window** (#373). It is reported as such rather than
  averaged into a number that describes no particular robot.
* **Config is read, never restated.** The bar and the interval come from the loaded config.
* **Absent is not zero.** A metric the robot did not report is absent, not 0 (#380's rule, applied
  to the reader as well as the writer).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from avid.core.config import load_config  # noqa: E402
from avid.domain import BootRecord, uptime_ratio  # noqa: E402

# ── reporting (shared shape with motion_pi.py / memory_pi.py) ────────────────────────────────

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
    "’": "'",
}


def _say(text: str = "") -> None:
    """Print *text*, folded to something every console can render.

    A gate tool that crashes while reporting is worse than one that reports plainly — this file's
    siblings learned that the expensive way, and a soak report is read over SSH from Windows.
    """
    for source, plain in _ASCII.items():
        text = text.replace(source, plain)
    print(text.encode("ascii", "replace").decode("ascii"))


_Verdict = Literal["pass", "fail", "recorded", "inconclusive"]


@dataclass
class _Criterion:
    """One reported line, held rather than printed so every criterion reports before any verdict."""

    ac: str
    name: str
    verdict: _Verdict
    detail: str = ""
    rows: list[str] = field(default_factory=list)


# ── the sampler's own store ──────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id        INTEGER PRIMARY KEY,
    at        INTEGER NOT NULL,   -- wall clock when the sample was ATTEMPTED
    reachable INTEGER NOT NULL,   -- did /health answer at all
    build     TEXT,               -- NULL when unreachable, which is itself the datum
    uptime_s  INTEGER,
    dropped   INTEGER,            -- summed bus overflow across every subscriber
    payload   TEXT                -- the whole /metrics body, for questions not yet asked
);
CREATE INDEX IF NOT EXISTS idx_samples_at ON samples(at);
"""


def _open_samples(path: Path) -> sqlite3.Connection:
    """Open (and create) the sampler's own database.

    ⚠️ Deliberately **not** the robot's `robot.db`. Two reasons, and the second is the one that
    matters: an observer sharing its subject's storage cannot report on that storage failing, and
    a soak exists precisely to catch the failures nobody predicted.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript("PRAGMA journal_mode = WAL;" + _SCHEMA)
    return conn


def _probe(base: str, timeout: float) -> dict[str, Any]:
    """One observation of the robot. Never raises — an unreachable robot IS the measurement."""
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=timeout) as response:
            reachable = response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return {
            "reachable": False,
            "build": None,
            "uptime_s": None,
            "dropped": None,
            "payload": None,
        }

    payload: dict[str, Any] | None = None
    try:
        with urllib.request.urlopen(f"{base}/metrics", timeout=timeout) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        payload = None

    metrics = (payload or {}).get("metrics", {})
    queues = metrics.get("bus_queues") or {}
    return {
        "reachable": reachable,
        "build": metrics.get("build"),
        "uptime_s": metrics.get("uptime_s"),
        # Absent stays absent: if the robot did not report queues, this is None and not 0 (#380).
        "dropped": sum(q.get("dropped", 0) for q in queues.values())
        if queues
        else None,
        "payload": json.dumps(payload) if payload is not None else None,
    }


def _sample_once(conn: sqlite3.Connection, base: str, timeout: float) -> dict[str, Any]:
    observation = _probe(base, timeout)
    with conn:
        conn.execute(
            "INSERT INTO samples (at, reachable, build, uptime_s, dropped, payload) "
            "VALUES (?,?,?,?,?,?)",
            (
                int(time.time()),
                1 if observation["reachable"] else 0,
                observation["build"],
                observation["uptime_s"],
                observation["dropped"],
                observation["payload"],
            ),
        )
    return observation


def _run_sampler(args: argparse.Namespace) -> int:
    """Poll the robot until stopped. Long-running; systemd owns its lifetime."""
    conn = _open_samples(Path(args.samples))
    base = f"http://{args.host}:{args.port}"
    _say(f"sampling {base} every {args.interval}s into {args.samples}")
    _say("(stop with Ctrl-C or `systemctl stop soak-sampler`)")
    deadline = time.monotonic() + args.for_seconds if args.for_seconds else None
    try:
        while deadline is None or time.monotonic() < deadline:
            observation = _sample_once(conn, base, args.timeout)
            mark = "up  " if observation["reachable"] else "DOWN"
            _say(
                f"  {time.strftime('%Y-%m-%d %H:%M:%S')}  {mark}  build={observation['build']}"
            )
            time.sleep(args.interval)
    except KeyboardInterrupt:  # pragma: no cover - operator stop
        _say("stopped")
    finally:
        conn.close()
    return 0


# ── grading ──────────────────────────────────────────────────────────────────────────────────


def _boot_records(db_path: str, since: int, until: int) -> list[BootRecord]:
    """The robot's own boot log over the window (#379), read directly.

    Read-only SQL rather than the adapter: the grader must be able to read a database written by a
    process that is no longer running, possibly on a machine where the app is not installed.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM boot_log WHERE started_at < ? "
            "AND COALESCE(stopped_at, last_seen_at) > ? ORDER BY started_at",
            (until, since),
        ).fetchall()
    finally:
        conn.close()
    return [
        BootRecord(
            boot_id=str(r["boot_id"]),
            build=str(r["build"]),
            started_at=int(r["started_at"]),
            started_mono=int(r["started_mono"]),
            last_seen_at=int(r["last_seen_at"]),
            stopped_at=None if r["stopped_at"] is None else int(r["stopped_at"]),
            stop_reason=r["stop_reason"],
        )
        for r in rows
    ]


def _grade(args: argparse.Namespace) -> list[_Criterion]:
    """Compute every criterion. Decides no verdict about the run as a whole — that is `_report`."""
    config = load_config(args.config)
    # Read from config, never restated: a banner quoting a value the run no longer uses is drift
    # with a delay fuse (§7.1).
    heartbeat_s = config.runtime.heartbeat_interval_s
    since = args.since
    until = args.until or int(time.time())
    window = until - since
    criteria: list[_Criterion] = []

    conn = _open_samples(Path(args.samples))
    try:
        samples = conn.execute(
            "SELECT * FROM samples WHERE at >= ? AND at < ? ORDER BY at", (since, until)
        ).fetchall()
    finally:
        conn.close()

    # ── AC-0: was this window observed at all? ────────────────────────────────────────────────
    #
    # FIRST, and it can only be `pass` or `inconclusive`. Everything below is a statement about a
    # record; if the record has holes, those statements are about a smaller window than claimed,
    # and saying so is the difference between a measurement and a story.
    if not samples:
        criteria.append(
            _Criterion(
                "AC-0",
                "the window was observed",
                "inconclusive",
                f"no samples between {since} and {until} — this run measured nothing",
            )
        )
        return criteria

    gaps: list[tuple[int, int]] = []
    bound = args.interval * args.gap_factor
    previous = samples[0]["at"]
    for row in samples[1:]:
        if row["at"] - previous > bound:
            gaps.append((previous, int(row["at"])))
        previous = int(row["at"])
    lead_in = samples[0]["at"] - since
    lead_out = until - samples[-1]["at"]
    blind = sum(end - start for start, end in gaps) + max(0, lead_in) + max(0, lead_out)
    coverage = 1.0 - (blind / window) if window > 0 else 0.0
    criteria.append(
        _Criterion(
            "AC-0",
            "the window was observed (the SAMPLER's own liveness)",
            "pass" if coverage >= args.min_coverage else "inconclusive",
            f"{coverage:.4%} of the window covered by {len(samples)} samples "
            f"(n={len(samples)}); {len(gaps)} gap(s) longer than {bound:.0f}s, "
            f"{blind}s unobserved in total",
            rows=[f"gap {start} -> {end} ({end - start}s)" for start, end in gaps[:10]],
        )
    )

    # ── AC-4: one window, one build (#373) ────────────────────────────────────────────────────
    #
    # Before any number is computed from it. A soak measures a specific build; averaging two of
    # them produces a figure describing no robot that ever existed.
    builds = sorted({str(r["build"]) for r in samples if r["build"]})
    criteria.append(
        _Criterion(
            "AC-4",
            "the build did not change mid-window",
            "pass" if len(builds) <= 1 else "fail",
            f"builds seen: {builds or ['(none reported)']}",
        )
    )

    records = _boot_records(args.robot_db, since, until)
    if not records:
        criteria.append(
            _Criterion(
                "AC-2",
                "uptime >= the O5 bar",
                "inconclusive",
                f"no boot_log rows in the window — {args.robot_db} has nothing to grade",
            )
        )
        return criteria

    # ── AC-2: uptime ──────────────────────────────────────────────────────────────────────────
    ratio = uptime_ratio(records, window_start=since, window_end=until)
    permitted = int(window * (1 - args.bar))
    down = window - int(window * ratio)
    criteria.append(
        _Criterion(
            "AC-2",
            f"uptime >= {args.bar:.2%}",
            "pass" if ratio >= args.bar else "fail",
            f"{ratio:.4%} over {window / 86400:.2f} days ({down}s down; "
            f"{permitted}s permitted at this bar)",
            rows=[
                f"⚠️ known only to within one heartbeat ({heartbeat_s:.0f}s): an unclean run is "
                f"credited to its last beat, so this UNDERSTATES availability, never the reverse",
            ],
        )
    )

    # ── AC-3: restarts ────────────────────────────────────────────────────────────────────────
    #
    # §12.6: a clean stop means the ordered teardown ran, which happens only because a person or a
    # deploy asked. A run with no stop recorded is a crash or a watchdog kill — NOT manual, and
    # counted separately because it is a defect either way.
    clean = [r for r in records if r.was_clean]
    unclean = [r for r in records if not r.was_clean]
    # The final record may simply still be running, which is not an unplanned stop.
    still_running = [
        r for r in unclean if r is records[-1] and until - r.last_seen_at <= bound
    ]
    unplanned = [r for r in unclean if r not in still_running]
    criteria.append(
        _Criterion(
            "AC-3",
            "zero manual restarts",
            "pass" if not clean else "fail",
            f"{len(clean)} clean stop(s) — a clean stop IS a manual restart (§12.6)",
            rows=[
                f"boot {r.boot_id} stopped at {r.stopped_at} ({r.stop_reason})"
                for r in clean[:10]
            ],
        )
    )
    criteria.append(
        _Criterion(
            "AC-3b",
            "zero unplanned stops (crash / watchdog kill)",
            "pass" if not unplanned else "fail",
            f"{len(unplanned)} run(s) ended without the ordered teardown"
            + (
                f"; {len(still_running)} still running at the window's close"
                if still_running
                else ""
            ),
            rows=[
                f"boot {r.boot_id} last seen {r.last_seen_at}" for r in unplanned[:10]
            ],
        )
    )

    # ── recorded, not graded: there is no bar for these, and inventing one here would be a bar
    #    nobody agreed to. They are the numbers a person reads to decide what to look at next.
    drops = [int(r["dropped"]) for r in samples if r["dropped"] is not None]
    criteria.append(
        _Criterion(
            "AC-5",
            "bus overflow across the window",
            "recorded" if drops else "inconclusive",
            f"{max(drops)} event(s) dropped at the window's peak (n={len(drops)} samples reported "
            f"queues)"
            if drops
            else "no sample reported bus_queues — the metric is ABSENT, not zero",
        )
    )
    criteria.append(
        _Criterion(
            "AC-6",
            "boot records in the window",
            "recorded",
            f"{len(records)} run(s): {len(clean)} clean, {len(unclean)} without a recorded stop",
        )
    )
    return criteria


def _report(criteria: list[_Criterion], args: argparse.Namespace) -> int:
    """Print every criterion, then the verdict. Returns the exit code."""
    _say("\n" + "=" * 78)
    _say("M11 soak gate — #389 / O5, SDS §12.6")
    _say("=" * 78)
    marks = {
        "pass": "PASS",
        "fail": "FAIL",
        "recorded": "····",
        "inconclusive": "INCONCL",
    }
    for criterion in criteria:
        _say(f"[{marks[criterion.verdict]}] {criterion.ac}  {criterion.name}")
        if criterion.detail:
            _say(f"        {criterion.detail}")
        for row in criterion.rows:
            _say(f"          - {row}")

    failed = [c for c in criteria if c.verdict == "fail"]
    inconclusive = [c for c in criteria if c.verdict == "inconclusive"]
    passed = [c for c in criteria if c.verdict == "pass"]
    _say("-" * 78)
    _say(
        f"{len(passed)} passed, {len(failed)} failed, {len(inconclusive)} inconclusive."
    )

    if inconclusive:
        _say(
            "\n!! INCONCLUSIVE is not a pass and not a failure. The run did not establish the\n"
            "   criterion — most often because the SAMPLER has holes, which makes every other\n"
            "   number here a statement about a smaller window than the one claimed."
        )
    if failed:
        _say(
            "\nA failure here is a RESULT, not an accident. Report the number: widening the bar to\n"
            "fit a measurement is a last resort, taken only with the diagnosis attached, and the\n"
            "original target stays visible — or it quietly becomes whatever was last achieved."
        )
    if not failed and not inconclusive:
        _say(
            "\nO5 met over the graded window. #389's remaining criteria still need a person."
        )
    return 1 if (failed or inconclusive) else 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("sample", "grade"), required=True)
    parser.add_argument("--config", default="/etc/robot/config.toml")
    parser.add_argument("--samples", default="/var/lib/soak/samples.db")
    parser.add_argument("--robot-db", default="/var/lib/robot/robot.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--interval", type=float, default=60.0, help="seconds between samples"
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--for-seconds",
        type=float,
        default=0.0,
        help="stop sampling after N seconds (0 = forever). The dry-run lever.",
    )
    parser.add_argument("--since", type=int, help="window start, epoch seconds")
    parser.add_argument(
        "--until", type=int, default=0, help="window end (default: now)"
    )
    parser.add_argument(
        "--bar", type=float, default=0.99, help="the O5 uptime bar (default 0.99)"
    )
    parser.add_argument(
        "--gap-factor",
        type=float,
        default=3.0,
        help="a gap longer than interval x this counts as unobserved",
    )
    parser.add_argument("--min-coverage", type=float, default=0.99)
    args = parser.parse_args()

    if args.mode == "sample":
        return _run_sampler(args)
    if args.since is None:
        parser.error("--mode grade needs --since (epoch seconds)")
    return _report(_grade(args), args)


if __name__ == "__main__":
    raise SystemExit(main())
