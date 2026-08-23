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
from collections.abc import Sequence
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

# How close a logged intervention must be to an unclean stop to be treated as its explanation.
# Generous: an operator writes the note before or after pulling the plug, not during.
_INTERVENTION_MATCH_S = 900

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

# Columns added after the table shipped (#404). ``CREATE TABLE IF NOT EXISTS`` is a no-op against
# an existing table, so a schema change made only up there is invisible on any database that
# already exists — and the first INSERT then dies with "no such column" days into a window.
_ADDED_COLUMNS = {
    # The robot's own resident set, and the machine's headroom. Both NULL when the robot did not
    # report them, which is a different fact from zero (#380) and stays distinguishable here.
    "rss_bytes": "INTEGER",
    "mem_available_bytes": "INTEGER",
    # What the robot was DOING, not merely whether it was up (#452). `state` is `GET /state`'s
    # RobotState name; `transitions` is the cumulative count from `GET /metrics`, which is
    # process-scoped and therefore resets on restart — see `_liveness_criterion` for why the pair
    # is needed and neither alone is enough.
    "state": "TEXT",
    "transitions": "INTEGER",
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring an existing ``samples`` table up to the current schema. Idempotent.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``, so the presence check is ``PRAGMA table_info``.
    Adding a nullable column is a metadata-only change — no rewrite, no risk to a database that
    already holds a window's worth of rows.
    """
    have = {row["name"] for row in conn.execute("PRAGMA table_info(samples)")}
    for column, kind in _ADDED_COLUMNS.items():
        if column not in have:
            conn.execute(f"ALTER TABLE samples ADD COLUMN {column} {kind}")


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
    with conn:
        _add_missing_columns(conn)
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
            "rss_bytes": None,
            "mem_available_bytes": None,
            "state": None,
            "transitions": None,
            "payload": None,
        }

    payload: dict[str, Any] | None = None
    try:
        with urllib.request.urlopen(f"{base}/metrics", timeout=timeout) as response:
            payload = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        payload = None

    # #452: the third route, and the one that says whether the robot is doing anything. A build
    # too old to serve `/state` — or one whose provider landed in `absent` — records NULL, which
    # `_liveness_criterion` reports as ABSENT rather than reading as a robot that never moved.
    # Its own try/except because a route that 404s must not cost us `/metrics`.
    reading: dict[str, Any] | None = None
    try:
        with urllib.request.urlopen(f"{base}/state", timeout=timeout) as response:
            reading = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        reading = None

    metrics = (payload or {}).get("metrics", {})
    queues = metrics.get("bus_queues") or {}
    return {
        "reachable": reachable,
        # `.get` on both: `/state` names an unreadable field in `absent` rather than defaulting it,
        # so a missing key here is the robot saying it could not answer — which is a different
        # fact from IDLE, and the whole reason `/state` was built that way.
        "state": (reading or {}).get("state"),
        "transitions": metrics.get("transitions"),
        "build": metrics.get("build"),
        "uptime_s": metrics.get("uptime_s"),
        # Absent stays absent: if the robot did not report queues, this is None and not 0 (#380).
        "dropped": sum(q.get("dropped", 0) for q in queues.values())
        if queues
        else None,
        # #404. `.get` returning None is exactly right: a robot too old to report memory, or one
        # whose provider landed in `absent`, records NULL rather than 0. The whole body is kept in
        # `payload` regardless, so the shape over time survives even for questions not asked yet.
        "rss_bytes": metrics.get("rss_bytes"),
        "mem_available_bytes": metrics.get("mem_available_bytes"),
        "payload": json.dumps(payload) if payload is not None else None,
    }


def _sample_once(conn: sqlite3.Connection, base: str, timeout: float) -> dict[str, Any]:
    observation = _probe(base, timeout)
    with conn:
        conn.execute(
            "INSERT INTO samples "
            "(at, reachable, build, uptime_s, dropped, rss_bytes, mem_available_bytes, "
            "state, transitions, payload) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                int(time.time()),
                1 if observation["reachable"] else 0,
                observation["build"],
                observation["uptime_s"],
                observation["dropped"],
                observation["rss_bytes"],
                observation["mem_available_bytes"],
                observation["state"],
                observation["transitions"],
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
            # The state rides the live line too (#452). Not because the grade pass needs it — it
            # reads the database — but because the wedge that voided the first window was found
            # by a human reading logs, and `up up up up` for an hour looks identical whether the
            # robot is conversing or catatonic.
            _say(
                f"  {time.strftime('%Y-%m-%d %H:%M:%S')}  {mark}  "
                f"build={observation['build']}  state={observation['state'] or '?'}"
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


def _read_interventions(path: Path) -> list[dict[str, Any]]:
    """The operator's own record of what they did to the robot (#389, SDS §12.6).

    §12.6 states the limit this exists for: *"The mechanism cannot distinguish a deploy from an
    operator `systemctl stop`: both are signals."* And a power cut cannot be distinguished from a
    crash at all — both leave `stopped_at` NULL and nothing else. The log is where a human writes
    down what the instrument structurally cannot see.

    One JSON object per line, appended by hand::

        {"at": 1787403600, "kind": "power_cut", "note": "unplugged the bench strip"}

    ⚠️ **Never raises and never grades.** A malformed line is skipped and counted, because an
    operator's typo at 2 a.m. must not take down the report for a thirty-day window.
    """
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    malformed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("at"), int):
            entries.append(parsed)
        else:
            malformed += 1
    if malformed:
        entries.append(
            {"at": 0, "kind": "_malformed", "note": f"{malformed} unreadable line(s)"}
        )
    return sorted(entries, key=lambda e: int(e["at"]))


def _intervention_criterion(
    entries: list[dict[str, Any]], unclean: list[BootRecord], window: tuple[int, int]
) -> _Criterion:
    """What the operator says they did, set beside what the robot recorded.

    ⚠️ **Reported, never graded — and this is the important property.** An operator note must not
    turn a red criterion green. *"Widening a budget to fit a measurement is a last resort, taken
    only with the diagnosis attached, and the original target stays visible"* (§7.1); a human
    typing "that one was me" is not a measurement at all. So AC-3 and AC-3b keep their verdicts and
    this line adds the context a reader needs to interpret them.

    What it *does* buy: an unplanned stop with a matching note reads as an explained event rather
    than an unexplained one, and thirty days from now nobody will remember which was which.
    """
    since, until = window
    inside = [
        e
        for e in entries
        if since <= int(e["at"]) < until or e.get("kind") == "_malformed"
    ]
    # ⚠️ The empty-log case must still list the unclean stops, and getting this wrong was caught by
    # its own test. An early return here meant the WORST case — stops with no explanation at all —
    # produced the LEAST information, which is the "hid a criterion" family (§7.1) reappearing
    # inside the thing written to prevent it.

    # An unclean stop near a logged intervention is explained; one that is not, is not.
    explained, rows = 0, []
    for record in unclean:
        near = [
            e
            for e in inside
            if abs(int(e["at"]) - record.ended_at) <= _INTERVENTION_MATCH_S
        ]
        if near:
            explained += 1
            rows.append(
                f"boot {record.boot_id[:8]} ended {record.ended_at} <- "
                f"{near[0].get('kind', '?')}: {near[0].get('note', '')}"
            )
        else:
            rows.append(
                f"boot {record.boot_id[:8]} ended {record.ended_at} <- UNEXPLAINED"
            )
    for entry in inside:
        rows.append(
            f"logged {entry['at']} {entry.get('kind', '?')}: {entry.get('note', '')}"
        )
    detail = (
        f"{len(inside)} logged; {explained} of {len(unclean)} unclean stop(s) have a matching "
        f"note within {_INTERVENTION_MATCH_S}s. AC-3/AC-3b keep their verdicts regardless — a "
        f"note explains an event, it does not excuse one."
    )
    if not inside:
        detail = (
            f"no interventions logged for this window, against {len(unclean)} unclean stop(s). "
            "⚠️ That is not the same as 'nobody touched it' — this log is written by hand, so an "
            "empty log and an unlogged power cut are indistinguishable here."
        )
    # Same cap, same rule as AC-3b: a truncated list must say it is truncated, or the report
    # quietly describes fewer events than it counted.
    shown = rows[:12]
    if len(rows) > len(shown):
        shown.append(
            f"... and {len(rows) - len(shown)} more not listed (capped at {len(shown)})"
        )
    return _Criterion(
        "INTV",
        "operator interventions (reported, not graded)",
        "recorded",
        detail,
        rows=shown,
    )


def _memory_criterion(samples: list[sqlite3.Row]) -> _Criterion:
    """What the window saw of memory. Always ``recorded`` — a report, not a verdict (#404).

    ⚠️ **A positive delta is not a leak**, and the detail line says so. Models here load lazily, the
    page cache grows into whatever is going spare, and glibc does not always return freed arenas to
    the kernel. What a reader is looking for is a *trend that does not flatten*, which is why the
    first/last pair and the extremes are printed rather than a single summary number — and why the
    full per-sample series stays in the `payload` column for anyone who wants to plot it.

    ⚠️ **Absent is not zero.** A window where the robot never reported memory says exactly that; it
    does not report 0 bytes resident, which is what a `sum() or 0` would have produced.
    """
    rss = [int(r["rss_bytes"]) for r in samples if r["rss_bytes"] is not None]
    available = [
        int(r["mem_available_bytes"])
        for r in samples
        if r["mem_available_bytes"] is not None
    ]
    if not rss and not available:
        return _Criterion(
            "MEM",
            "memory over the window (reported, not graded)",
            "recorded",
            f"no memory readings in {len(samples)} sample(s) — the robot did not report "
            f"rss_bytes or mem_available_bytes. Absent, NOT zero: check that the build under "
            f"test carries #404 and that /metrics does not list them in `absent`.",
        )

    mib = 1024 * 1024
    rows: list[str] = []
    if rss:
        delta = rss[-1] - rss[0]
        rows.append(
            f"robot RSS       n={len(rss)}  first {rss[0] / mib:.1f} MiB  "
            f"last {rss[-1] / mib:.1f} MiB  delta {delta / mib:+.1f} MiB  "
            f"min {min(rss) / mib:.1f}  max {max(rss) / mib:.1f}"
        )
    else:
        rows.append("robot RSS       absent from every sample")
    if available:
        rows.append(
            f"machine avail   n={len(available)}  first {available[0] / mib:.1f} MiB  "
            f"last {available[-1] / mib:.1f} MiB  "
            f"min {min(available) / mib:.1f}  max {max(available) / mib:.1f}"
        )
    else:
        rows.append("machine avail   absent from every sample")
    return _Criterion(
        "MEM",
        "memory over the window (reported, not graded)",
        "recorded",
        "a rising delta is not by itself a leak - lazy model loads, page cache and glibc "
        "arenas all move it. Look for a trend that never flattens.",
        rows=rows,
    )


# ── LIVE: was the robot DOING anything (#452 AC-4) ───────────────────────────────────────────
#
# The criterion this harness was missing, and the reason its first window was void. For ~26 hours
# it graded a robot that entered THINKING at minute 3 and never left: 147 of the process's 175 log
# lines were `ignored illegal transition ... in state THINKING`, and AC-2 read **99.89% uptime**
# with every graded criterion passing. Every instrument here measured the *process* — heartbeat,
# RSS, watchdog, queue drops — and all of those are satisfied by a robot doing nothing at all.
# That is M8's lesson in a new costume (a vision gate passed while blind), and a window that
# cannot tell a working robot from a catatonic one is not evidence for O5 at any duration.
#
# ⚠️ **Neither available signal is sufficient alone, and the reason is worth keeping.**
#
# * `GET /state` gives the state at each sample. But identical readings 60 s apart do NOT prove
#   the state was *held*: a talking robot cycles THINKING -> SPEAKING -> IDLE -> LISTENING ->
#   THINKING between two samples and looks exactly like a wedged one.
# * `/metrics`' `transitions` counter proves movement. But it is cumulative and process-scoped, so
#   it resets on restart, and a low count is ambiguous on its own — an empty house at 3 a.m.
#   legitimately produces none.
#
# Together they are conclusive: a run of consecutive samples reading the same state **with the
# transitions counter unchanged across the whole run** proves the machine did not move for the
# span of that run. That conjunction is what this grades.

# The states the design says are TRANSIENT, and what bounds each. A value of None means "no bound
# exists to grade against" — reported, never graded, because inventing a bar here would be a bar
# nobody agreed to (and §12.6's own rule is that this file computes O5's terms, it does not
# re-decide them).
#
# ⚠️ LISTENING is None for a reason worth printing rather than hiding: SDS §3.10.3 documents a
# 30 s listen timeout and `Trigger.LISTEN_TIMEOUT` is **unwired** — the row exists and nothing
# drives it, which is the exact shape #452 was (a row that existed, reachable from one arc only).
# When it gains a driver and a config key, it belongs in this table.
_TRANSIENT_STATES: dict[str, str | None] = {
    # The §6.9 first-token deadline. Read from config, never restated (§7.1).
    "THINKING": "think_timeout_s",
    "LISTENING": None,  # Trigger.LISTEN_TIMEOUT is unwired — see above
    "SPEAKING": None,  # bounded by the reply's own length, which is not a config value
    "BOOTING": None,  # bounded by adapter start-up, which nothing states as a number
}

# Not transient, and not a fault: SDS §12.6 is explicit that **a SLEEPING robot is UP**, and IDLE
# is where a companion spends most of a quiet night. Held for hours legitimately, so held-time is
# reported for them and never graded. DEGRADED is neither — recovery is rising-edge driven (§6.9),
# so a robot alone in a room stays degraded until someone speaks, and that can honestly be hours.
_RESTING_STATES = ("IDLE", "SLEEPING", "DEGRADED")


@dataclass(frozen=True, slots=True)
class _StateRun:
    """A stretch of consecutive samples that read the same state and PROVABLY did not move.

    ``proven`` is the whole point. A run is proven only when every consecutive pair inside it
    reported the ``transitions`` counter and reported it **unchanged** — that is what separates
    "the machine sat here" from "the machine cycled between our samples". An unproven run is still
    printed; it simply cannot fail anything, because it does not establish what it would fail on.
    """

    state: str
    first_at: int
    last_at: int
    samples: int
    proven: bool
    # The robot's own uptime delta across the run, when it reported uptime at both ends. Preferred
    # over the `at` delta because it is immune to the wall clock this board cannot be trusted with
    # (#439) — within one process it only ever counts forward.
    held_by_uptime_s: int | None

    @property
    def held_s(self) -> int:
        """How long the state was held, by the most trustworthy reading available."""
        if self.held_by_uptime_s is not None:
            return self.held_by_uptime_s
        return self.last_at - self.first_at

    @property
    def from_wall_clock(self) -> bool:
        """Did :attr:`held_s` come from the untrusted wall clock? Then say so beside the number."""
        return self.held_by_uptime_s is None


def _state_runs(samples: Sequence[sqlite3.Row], *, gap_bound: float) -> list[_StateRun]:
    """Split the window into maximal runs of "the same state, provably not moving".

    A run is broken by anything that makes "it stayed here" unsafe to claim:

    * the state changed, or a sample did not report one;
    * the ``transitions`` counter moved — the robot cycled through other states between two
      samples, which is a *working* robot and must never be graded as a stuck one;
    * the process restarted (``build`` changed, or ``uptime_s`` fell) — a new process is a new
      subject, and its counter starts again from zero;
    * the sampler was not looking (a gap wider than AC-0's own bound). Anything could have
      happened in a hole, so a run cannot be claimed across one. ⚠️ This direction matters: not
      splitting would *overstate* held time and could fail a healthy robot on the sampler's outage.
    """
    runs: list[_StateRun] = []
    start: sqlite3.Row | None = None
    previous: sqlite3.Row | None = None
    count = 0
    proven = True

    def _flush() -> None:
        if start is None or previous is None or count < 2:
            # A single sample is a reading, not a duration: it establishes no held time at all.
            return
        held: int | None = None
        if start["uptime_s"] is not None and previous["uptime_s"] is not None:
            held = int(previous["uptime_s"]) - int(start["uptime_s"])
        runs.append(
            _StateRun(
                state=str(start["state"]),
                first_at=int(start["at"]),
                last_at=int(previous["at"]),
                samples=count,
                proven=proven,
                held_by_uptime_s=held,
            )
        )

    for row in samples:
        state = row["state"]
        if state is None:
            _flush()
            start = previous = None
            count = 0
            proven = True
            continue
        if previous is not None and _continues(previous, row, gap_bound=gap_bound):
            count += 1
            proven = proven and _proves_stillness(previous, row)
            previous = row
            continue
        _flush()
        start = previous = row
        count = 1
        proven = True
    _flush()
    return runs


def _continues(previous: sqlite3.Row, row: sqlite3.Row, *, gap_bound: float) -> bool:
    """Can *row* extend a run that *previous* is in? Same state, same process, no unobserved hole."""
    if previous["state"] != row["state"]:
        return False
    if previous["build"] != row["build"]:
        return False
    if previous["uptime_s"] is not None and row["uptime_s"] is not None:
        # A fall means a new process. A rise means the same one lived through the step — the same
        # reading `_ClockStep.robot_restarted` uses, and for the same reason (#439).
        elapsed = int(row["uptime_s"]) - int(previous["uptime_s"])
        if elapsed < 0:
            return False
        # ⚠️ The gap test runs on the ROBOT's clock when it can. `at` is the column #439 caught
        # going backwards between consecutive writes, and a backwards step followed by a forward
        # one would chop a real run in half — under-reporting exactly the held time this grades.
        # `uptime_s` measures the same elapsed seconds and cannot do that within one process.
        return elapsed <= gap_bound
    return int(row["at"]) - int(previous["at"]) <= gap_bound


def _proves_stillness(previous: sqlite3.Row, row: sqlite3.Row) -> bool:
    """Did the machine demonstrably NOT move between these two samples?

    Only when both reported ``transitions`` and the count is identical. An absent counter proves
    nothing either way — and reading absent as "unchanged" would manufacture a wedge out of a
    build too old to report it, which is #380's rule pointed at the grader instead of the sampler.
    """
    if previous["transitions"] is None or row["transitions"] is None:
        return False
    return int(previous["transitions"]) == int(row["transitions"])


def _transition_total(samples: Sequence[sqlite3.Row]) -> int | None:
    """How many state changes the window saw, summed across processes. ``None`` if never reported.

    The counter is process-scoped, so a restart resets it: the window's total is the sum of the
    per-process *rises*, and a fall is a new process rather than a negative count.
    """
    counts = [int(r["transitions"]) for r in samples if r["transitions"] is not None]
    if not counts:
        return None
    total = counts[0]
    for before, after in zip(counts, counts[1:]):
        total += after - before if after >= before else after
    return total


def _liveness_criterion(
    samples: Sequence[sqlite3.Row],
    *,
    bounds: dict[str, float],
    interval: float,
    gap_bound: float,
) -> _Criterion:
    """LIVE — did the robot ever do anything, and did it hold a state it promises it cannot (#452).

    **Graded**, unlike MEM and CLOCK, and the bar is defensible precisely because it is not
    invented here: a transient state has a bound the design already states, in config, and
    exceeding it is a defect the state machine promises cannot happen. THINKING's bound is
    ``[gate] think_timeout_s`` — the §6.9 deadline — and the wedge that voided the first window
    exceeded it by a factor of **8,600**.

    One sampling interval of slack is added to every bound, and printed. The sampler sees the
    machine at its own cadence, so a state entered just after one sample and left just before the
    next is indistinguishable from one held the whole time; the slack is that uncertainty made
    explicit rather than argued away.

    ⚠️ **Verdict `inconclusive`, never `pass`, when the robot did not report `/state` or
    `transitions`.** A window with no state series did not establish liveness — it is exactly the
    catatonic case that produced 99.89% uptime, read through an instrument that was not there. A
    `0` from an absent instrument reads exactly like a real `0`, which is this project's most
    expensive recurring defect.

    ⚠️ **The transition count is reported, not graded**, and that asymmetry is deliberate. A
    minimum count cannot be defended: an empty house produces no transitions, and a criterion that
    fails because nobody came home is a criterion people learn to ignore. What *can* be defended is
    the bounded state, which is why that is the half with teeth.
    """
    stated = [r for r in samples if r["state"] is not None]
    total_transitions = _transition_total(samples)
    if not stated:
        return _Criterion(
            "LIVE",
            "the robot was doing something (#452)",
            "inconclusive",
            f"no sample in {len(samples)} reported a state — GET /state answered nothing. "
            f"ABSENT, not idle: check that the build under test carries #385's /state route and "
            f"that `state` is not listed in the response's `absent`. A window with no state "
            f"series cannot tell a working robot from a wedged one, which is the whole of #452.",
        )

    runs = _state_runs(stated, gap_bound=gap_bound)
    seen = sorted({str(r["state"]) for r in stated})
    rows: list[str] = [
        f"states seen     {', '.join(seen)}  (n={len(stated)} samples reported a state, "
        f"{len(samples) - len(stated)} did not)",
        f"transitions     {total_transitions if total_transitions is not None else 'ABSENT'}"
        f"  (reported, not graded — an empty house legitimately produces none)",
    ]

    longest: dict[str, _StateRun] = {}
    for run in runs:
        current = longest.get(run.state)
        if current is None or run.held_s > current.held_s:
            longest[run.state] = run
    for state in sorted(longest):
        run = longest[state]
        source = "wall clock" if run.from_wall_clock else "robot uptime"
        proof = "proven still" if run.proven else "NOT proven still"
        rows.append(
            f"longest {state:<9} {run.held_s}s over {run.samples} samples "
            f"({source}, {proof})"
        )

    # Only a proven run can fail anything: an unproven one has not established that the machine
    # stayed put, and failing on it would be convicting on the absence of evidence.
    breaches = [
        (run, bounds[run.state])
        for run in runs
        if run.proven
        and run.state in bounds
        and run.held_s > bounds[run.state] + interval
    ]
    for run, bound in breaches[:10]:
        rows.append(
            f"!! {run.state} held {run.held_s}s at {run.first_at} — its bound is {bound:g}s "
            f"(+{interval:g}s sampling slack). The machine did not move for the whole run."
        )
    if len(breaches) > 10:
        rows.append(
            f"... and {len(breaches) - 10} more not listed — the count above is the whole figure"
        )

    gradeable = {state: bounds[state] for state in _TRANSIENT_STATES if state in bounds}
    ungraded = [state for state, key in _TRANSIENT_STATES.items() if key is None]
    rows.append(
        f"graded bounds   {gradeable}  (+{interval:g}s slack); no bound exists for "
        f"{', '.join(ungraded)}, so those are reported only"
    )
    rows.append(
        f"resting states  {', '.join(_RESTING_STATES)} are never graded on held time — S12.6 is "
        f"explicit that a SLEEPING robot is UP"
    )

    if total_transitions is None:
        return _Criterion(
            "LIVE",
            "the robot was doing something (#452)",
            "inconclusive",
            "the robot reported states but no `transitions` counter, so no run can be PROVEN "
            "still: identical readings 60s apart are equally consistent with a wedged robot and "
            "a talking one. ABSENT, not zero.",
            rows=rows,
        )
    return _Criterion(
        "LIVE",
        "the robot was doing something (#452)",
        "fail" if breaches else "pass",
        f"{len(breaches)} breach(es) of a designed bound across {len(runs)} run(s) of >=2 "
        f"samples in one state"
        if breaches
        else f"no transient state was provably held past its bound, across {len(runs)} run(s) "
        f"of >=2 samples in one state",
        rows=rows,
    )


# ── the clock every other number here is written in (#439) ───────────────────────────────────
#
# Every figure this harness prints is a subtraction between two readings of the Pi's wall clock,
# and this board has no RTC. An offline boot restores a stale time and NTP steps it later —
# observed 2026-08-22, twice inside one boot, the first from a clock reading 2026-04-27. The
# soak's own evidence caught it: ordered by ROWID, two consecutive samples read 13:41:08 then
# 13:41:07, and the earlier-written row carried the OLD robot process's uptime still counting.
#
# ⚠️ Reported, never corrected and never graded. The observed disagreement is seconds against a
# 7h12m budget, and a harness that repaired `at` would be inventing the measurement it exists to
# take. What it can honestly do is say the arithmetic spans more than one clock, and stop calling
# the result a measurement.

# How far past the window's edge the detector reads, expressed in seconds and converted with the
# sampler's OWN cadence rather than a row count, so it means the same thing at any interval.
_CLOCK_EDGE_S = 3600.0


@dataclass(frozen=True, slots=True)
class _ClockStep:
    """One place ``at`` went BACKWARDS between two consecutive WRITES (rowid order).

    Rowid is the only ordering that survives here: it is assigned by the insert, so it records the
    order the sampler actually wrote in, whatever the clock said at the time.
    """

    row_id: int
    before_at: int
    after_at: int
    uptime_before: int | None
    uptime_after: int | None

    @property
    def backwards_s(self) -> int:
        """How far back the clock jumped.

        ⚠️ **Not a duration.** It is the disagreement between two readings of an untrusted clock;
        no real time is being measured. Printed with a unit only because the reading has one.
        """
        return self.before_at - self.after_at

    @property
    def robot_restarted(self) -> bool | None:
        """Did a NEW robot process begin across this step? ``None`` when it cannot be told.

        ``uptime_s`` is the robot's own count, so it is immune to the wall clock: a *fall* means a
        new process, a *rise* means the same process lived through the step and the machine's
        clock moved underneath it. An absent reading on either side answers neither question, and
        `int(x or 0)` here would manufacture a restart out of a missing number (#380).
        """
        if self.uptime_before is None or self.uptime_after is None:
            return None
        return self.uptime_after < self.uptime_before


def _read_in_write_order(
    conn: sqlite3.Connection, *, since: int, until: int, interval: float
) -> list[sqlite3.Row]:
    """Re-read the samples in WRITE order, deliberately not filtered by the clock under audit.

    ``_grade``'s own read cannot be reused for this, for two independent reasons:

    * it is ``ORDER BY at``, which sorts a backwards step back into ascending order — the defect
      becomes invisible in the act of reading it; and
    * it is ``WHERE at >= ? AND at < ?``, which filters by *the very clock being audited*, so a
      row whose stale ``at`` fell outside the window is dropped before anything can notice.

    So the anchors are taken once (the only place the untrusted clock is consulted, and only at
    the two edges), and the rows are then fetched **by rowid with no ``at`` predicate at all**.
    Everything written *between* two in-window rows is therefore captured however wrong its
    timestamp is — that is the blind spot closed. Rows written before the first or after the last
    in-window row could only be attributed to this window by trusting the clock under audit, so
    the reach past each edge is bounded at :data:`_CLOCK_EDGE_S`, and the criterion prints how
    many rows it actually looked at rather than implying it saw everything.
    """
    edge = max(1, int(_CLOCK_EDGE_S // max(interval, 1.0)))
    anchors = conn.execute(
        "SELECT MIN(id) AS lo, MAX(id) AS hi FROM samples WHERE at >= ? AND at < ?",
        (since, until),
    ).fetchone()
    if anchors is None or anchors["lo"] is None:
        # No sample's `at` lands in the window at all. That is not "no data" — it is exactly what
        # a clock stale by months looks like through a `WHERE at` filter, so fall back to the most
        # recent rows by write order and let the criterion say what it is looking at.
        rows = conn.execute(
            "SELECT id, at, reachable, build, uptime_s FROM samples ORDER BY id DESC LIMIT ?",
            (edge,),
        ).fetchall()
        return list(reversed(rows))
    return list(
        conn.execute(
            "SELECT id, at, reachable, build, uptime_s FROM samples "
            "WHERE id >= ? AND id <= ? ORDER BY id",
            (int(anchors["lo"]) - edge, int(anchors["hi"]) + edge),
        ).fetchall()
    )


def _clock_steps(rows: Sequence[sqlite3.Row]) -> list[_ClockStep]:
    """Every place the clock went backwards between consecutive writes.

    Strictly backwards. Two samples stamped the same second are not a step — the sampler can write
    twice inside one second and that says nothing about the clock. Forward jumps are **not**
    collected: an NTP correction forward and a sampler that simply stopped for a while are not
    separable without a monotonic reading in ``samples``, which the schema does not have. AC-0
    already surfaces those as gaps, and guessing between the two here would be inventing a fact.
    """
    steps: list[_ClockStep] = []
    for previous, row in zip(rows, rows[1:]):
        if int(row["at"]) < int(previous["at"]):
            steps.append(
                _ClockStep(
                    row_id=int(row["id"]),
                    before_at=int(previous["at"]),
                    after_at=int(row["at"]),
                    uptime_before=(
                        None
                        if previous["uptime_s"] is None
                        else int(previous["uptime_s"])
                    ),
                    uptime_after=(
                        None if row["uptime_s"] is None else int(row["uptime_s"])
                    ),
                )
            )
    return steps


def _boot_mono_starts(db_path: str) -> list[tuple[str, int]]:
    """``(boot_id, started_mono)`` for every recorded run, in WRITE order.

    ``started_mono`` is ``time.monotonic_ns()`` taken at process start, and it is the one column
    in the whole record that a clock step cannot touch. It has been written since #379 and read by
    nothing until now.

    ⚠️ **Never raises.** A report *about* the instrument must not die with the instrument: a
    missing or unreadable ``robot.db`` yields no evidence, not no report. ``_boot_records`` still
    fails loudly where it always did — this is a second, additive read.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT boot_id, started_mono FROM boot_log ORDER BY rowid"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [(str(row[0]), int(row[1])) for row in rows]


def _mono_reboots(
    starts: Sequence[tuple[str, int]],
) -> list[tuple[str, str, int, int]]:
    """Consecutive runs whose ``started_mono`` DECREASED — i.e. a new monotonic origin.

    Monotonic time only grows within one machine boot, so a decrease is **proof** the machine
    rebooted between the two runs. This is what distinguished a reboot from a service restart in
    #439: 48.7 s against the previous run's 15726 s.

    ⚠️ The converse is not true and this must never be read as one. An increase is consistent with
    a service restart on a machine that stayed up, but a reboot whose successor happened to start
    later on the new clock than its predecessor did on the old one looks identical. Absence of
    proof, and the criterion says so in those words.

    Strict ``<``: equal values prove nothing, and a row written before the column meant anything
    reads as 0.
    """
    reboots: list[tuple[str, str, int, int]] = []
    for (previous_id, previous_mono), (this_id, this_mono) in zip(starts, starts[1:]):
        if this_mono < previous_mono:
            reboots.append((previous_id, this_id, previous_mono, this_mono))
    return reboots


def _clock_criterion(
    rows: Sequence[sqlite3.Row],
    steps: Sequence[_ClockStep],
    reboots: Sequence[tuple[str, str, int, int]],
    *,
    window: tuple[int, int],
) -> _Criterion:
    """The clock's own continuity, reported so the figures above can be read honestly."""
    since, until = window
    outside = sum(1 for row in rows if not (since <= int(row["at"]) < until))
    frames = len(steps) + 1
    lines: list[str] = []
    for step in steps:
        restarted = step.robot_restarted
        if restarted is None:
            verdict = "robot uptime absent on one side - CANNOT TELL whether the robot restarted"
        elif restarted:
            verdict = (
                f"robot uptime {step.uptime_before}s -> {step.uptime_after}s = "
                "a NEW ROBOT PROCESS began in this interval"
            )
        else:
            verdict = (
                f"robot uptime {step.uptime_before}s -> {step.uptime_after}s = the robot "
                "process SURVIVED (the machine's clock moved under a running robot)"
            )
        lines.append(
            f"step at rowid {step.row_id}: at {step.before_at} -> {step.after_at} "
            f"({step.backwards_s}s backwards); {verdict}"
        )
    for previous_id, this_id, previous_mono, this_mono in reboots:
        lines.append(
            f"boot {this_id[:8]} started_mono {this_mono / 1e9:.1f}s after {previous_id[:8]}'s "
            f"{previous_mono / 1e9:.1f}s - a DECREASE, so a new monotonic origin: the MACHINE "
            "REBOOTED between these two runs"
        )
    if not steps:
        detail = (
            f"no backwards step in {len(rows)} row(s) read in write order ({outside} of them "
            f"outside the graded `at` range); the window is ONE clock frame. "
            "!! this compares `at` against WRITE order only - a step FORWARD is indistinguishable "
            "from a sampler gap without a monotonic column in `samples`, and shows up above as an "
            "AC-0 gap instead"
        )
    else:
        detail = (
            f"{len(steps)} backwards step(s) in write order; the window spans {frames} clock "
            f"frames. Read {len(rows)} row(s) by rowid, {outside} of them outside the graded "
            "`at` range and therefore invisible to every other criterion here. Reported, never "
            "graded and never corrected - AC-0 and AC-2 keep their verdicts and carry a caveat "
            "row. !! epoch arithmetic across a frame boundary is not a duration (AVID-345, #439)"
        )
    if reboots and not steps:
        detail += (
            f" - but {len(reboots)} machine reboot(s) are PROVEN by started_mono, so the runs "
            "AC-2 sums still span a clock discontinuity"
        )
    return _Criterion(
        "CLOCK",
        "wall-clock continuity across the window (reported, not graded)",
        "recorded",
        detail,
        rows=lines[:12],
    )


def _clock_caveat_rows(
    steps: Sequence[_ClockStep],
    reboots: Sequence[tuple[str, str, int, int]],
    *,
    criterion: Literal["AC-0", "AC-2"],
) -> list[str]:
    """The caveat AC-0 and AC-2 carry when their arithmetic crossed a clock frame.

    Empty when the window is one frame and no reboot is proven — the caveat has to *discriminate*,
    or it is decoration that would read as a warning on a clean run and teach a reader to skip it.

    The trigger is deliberately a superset of ``frames > 1``: a proven machine reboot means AC-2
    summed ``started_at``/``last_seen_at`` spans across a discontinuity even if no sample happened
    to straddle it.
    """
    if not steps and not reboots:
        return []
    moved = (
        f"the wall clock moved BACKWARDS {len(steps)} time(s)"
        if steps
        else "the machine rebooted"
    )
    if criterion == "AC-0":
        return [
            f"!! {moved} in this window (see CLOCK): every gap above is a subtraction between "
            "two readings of that clock, so a gap spanning a frame boundary is not a duration "
            "and the unobserved total is an estimate, not a measurement"
        ]
    return [
        f"!! {moved} / {len(reboots)} machine reboot(s) are proven by started_mono (see CLOCK): "
        "this figure sums boot_log's wall-clock started_at and last_seen_at, so any run spanning "
        f"a frame boundary contributes a span the clock cannot vouch for. S12.6's arithmetic "
        f"assumes ONE clock frame; this window had {len(steps) + 1}"
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
        # ⚠️ A second read, and it must be a second read: the query above is ordered and filtered
        # by the clock this one audits, so it can neither show a backwards step nor see a row the
        # step pushed outside the window (#439).
        written = _read_in_write_order(
            conn, since=since, until=until, interval=args.interval
        )
    finally:
        conn.close()

    steps = _clock_steps(written)
    reboots = _mono_reboots(_boot_mono_starts(args.robot_db))
    clock = _clock_criterion(written, steps, reboots, window=(since, until))

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
        # ⚠️ Before the return, not after it. "No sample's `at` lands in the window" is precisely
        # what a clock stale by months looks like through a `WHERE at` filter, so this is the one
        # branch where the clock line is most likely to be the explanation — and hiding it behind
        # an unrelated inconclusive is the defect §7.1 names.
        criteria.append(clock)
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
            rows=[f"gap {start} -> {end} ({end - start}s)" for start, end in gaps[:10]]
            + _clock_caveat_rows(steps, reboots, criterion="AC-0"),
        )
    )
    criteria.append(clock)

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

    # ── MEM: memory over the window (#404) — REPORTED, NEVER GRADED ───────────────────────────
    #
    # Placed here deliberately: it depends only on `samples`, so it must report BEFORE the
    # boot_log early-return below. Hiding it behind an unrelated inconclusive would be the same
    # defect as a reporter that returns on its first failure (§7.1).
    #
    # O5's criteria are uptime and restarts (SDS §12.6) and #404 does not amend them, so the
    # verdict is always `recorded` — it renders as `····` and cannot move the exit code. A leak is
    # still the one failure class thirty days can find and fifteen minutes cannot, so the number
    # gets printed either way.
    criteria.append(_memory_criterion(samples))

    # ── LIVE: was the robot doing anything (#452 AC-4) — GRADED ───────────────────────────────
    #
    # Beside MEM and for the same structural reason: it depends only on `samples`, so it reports
    # before the boot_log early-return below. Unlike MEM it is graded, and the bar is read from
    # config rather than stated here — `[gate] think_timeout_s` is the §6.9 deadline, and a
    # THINKING run that outlives it is a wedge by the state machine's own definition.
    criteria.append(
        _liveness_criterion(
            samples,
            bounds={
                state: float(getattr(config.gate, key))
                for state, key in _TRANSIENT_STATES.items()
                if key is not None
            },
            interval=args.interval,
            gap_bound=bound,
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
            ]
            + _clock_caveat_rows(steps, reboots, criterion="AC-2"),
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
        _intervention_criterion(
            _read_interventions(Path(args.interventions)), unplanned, (since, until)
        )
    )
    # ⚠️ The verdict is deliberately binary — one unplanned stop and thirty read `fail` alike —
    # because §12.6 grades O5 on uptime and *manual* restarts, and an unclean stop is neither. The
    # COUNT is what a reader acts on (#439 AC-5), so it must survive the row cap: an eleventh stop
    # that vanished from the report while the policy says the count is the point would be this
    # harness's own "report the quantity you grade" rule broken from the inside.
    shown = unplanned[:10]
    unplanned_rows = [f"boot {r.boot_id} last seen {r.last_seen_at}" for r in shown]
    if len(unplanned) > len(shown):
        unplanned_rows.append(
            f"... and {len(unplanned) - len(shown)} more not listed — the count above is the "
            f"whole figure, this list is capped at {len(shown)}"
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
            rows=unplanned_rows,
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
    parser.add_argument(
        "--interventions",
        default="/var/lib/soak/interventions.jsonl",
        help="operator's own log of what they did to the robot (#389, SDS S12.6) - one JSON "
        "object per line. Reported, never graded.",
    )
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
