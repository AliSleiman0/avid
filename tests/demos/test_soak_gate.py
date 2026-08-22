"""The M11 soak gate's own gate (#410, SDS §12.6).

M3, M4, M5, M7 and M9 each have a test over their harness. M11 did not — and it is the one whose
defects cannot be found by re-running it. `soak_pi.py`'s own docstring names the fear: *"producing
thirty days of data that turns out not to mean anything."* A grader bug surfaces on day thirty, and
the remedy is another thirty days.

⚠️ **These drive the real `_grade`**, with a real config, a real samples DB and a real `boot_log` —
not extracted fragments. A test over a copy of the expression keeps passing when the original
drifts, which is the failure mode it would be here to prevent.

Every case is a way the window could produce a number that means something other than it says.
"""

from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SIM_TOML = str(_ROOT / "config" / "sim.toml")


def _load_soak() -> Any:
    spec = importlib.util.spec_from_file_location(
        "soak_pi_gate", _ROOT / "docs" / "demos" / "soak_pi.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["soak_pi_gate"] = module
    spec.loader.exec_module(module)
    return module


soak = _load_soak()

_DAY = 86_400
_SINCE = 1_700_000_000
_UNTIL = _SINCE + 30 * _DAY
_BUILD = "v0.M10.0-41-gf2e8e74"

_BOOT_SCHEMA = """
CREATE TABLE boot_log (
    boot_id      TEXT PRIMARY KEY,
    build        TEXT NOT NULL,
    started_at   INTEGER NOT NULL,
    started_mono INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    stopped_at   INTEGER,
    stop_reason  TEXT
);
"""


def _robot_db(tmp_path: Path, boots: list[dict[str, Any]]) -> str:
    path = tmp_path / "robot.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_BOOT_SCHEMA)
    for i, b in enumerate(boots):
        conn.execute(
            "INSERT INTO boot_log VALUES (?,?,?,?,?,?,?)",
            (
                b.get("boot_id", f"boot-{i}"),
                b.get("build", _BUILD),
                b["started_at"],
                b.get("started_mono", 0),
                b["last_seen_at"],
                b.get("stopped_at"),
                b.get("stop_reason"),
            ),
        )
    conn.commit()
    conn.close()
    return str(path)


def _samples_db(
    tmp_path: Path, ats: list[int], *, builds: list[str] | None = None
) -> str:
    path = tmp_path / "samples.db"
    conn = soak._open_samples(path)
    with conn:
        for i, at in enumerate(ats):
            conn.execute(
                "INSERT INTO samples (at, reachable, build, uptime_s, dropped) "
                "VALUES (?,1,?,60,0)",
                (at, (builds[i] if builds else _BUILD)),
            )
    conn.close()
    return str(path)


def _args(samples: str, robot_db: str, **over: Any) -> argparse.Namespace:
    base = dict(
        config=_SIM_TOML,
        samples=samples,
        robot_db=robot_db,
        since=_SINCE,
        until=_UNTIL,
        interval=60.0,
        gap_factor=3.0,
        min_coverage=0.99,
        bar=0.99,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _by_ac(criteria: list[Any]) -> dict[str, Any]:
    return {c.ac: c for c in criteria}


def _dense(step: int = 60) -> list[int]:
    """A fully-covered window: a sample every `step` seconds, edge to edge."""
    return list(range(_SINCE, _UNTIL, step))


# ── AC-0: the sampler's own liveness, which is the worst thing to get wrong ──────────────────


def test_no_samples_at_all_is_inconclusive_not_a_pass(tmp_path: Path) -> None:
    """An unobserved window measured nothing, and must not read as a clean run."""
    args = _args(_samples_db(tmp_path, []), _robot_db(tmp_path, []))
    criteria = soak._grade(args)
    assert _by_ac(criteria)["AC-0"].verdict == "inconclusive"
    assert soak._report(criteria, args) != 0


def test_a_sampler_that_died_mid_window_is_inconclusive(tmp_path: Path) -> None:
    """⚠️ The single worst outcome this harness names: *"a soak reporting 100% uptime because its
    sampler died on day three."*

    The robot may have been perfectly up. The point is that nobody was watching, so the figure
    describes three days while claiming thirty.
    """
    died_on_day_three = list(range(_SINCE, _SINCE + 3 * _DAY, 60))
    args = _args(
        _samples_db(tmp_path, died_on_day_three),
        _robot_db(tmp_path, [{"started_at": _SINCE, "last_seen_at": _UNTIL}]),
    )
    ac0 = _by_ac(soak._grade(args))["AC-0"]
    assert ac0.verdict == "inconclusive", ac0.detail
    assert "covered" in ac0.detail


def test_a_fully_covered_window_passes_ac0(tmp_path: Path) -> None:
    """The other end. A checker that called everything inconclusive would satisfy the two above."""
    args = _args(
        _samples_db(tmp_path, _dense()),
        _robot_db(tmp_path, [{"started_at": _SINCE, "last_seen_at": _UNTIL}]),
    )
    ac0 = _by_ac(soak._grade(args))["AC-0"]
    assert ac0.verdict == "pass", ac0.detail


def test_a_hole_in_the_middle_is_counted_as_unobserved(tmp_path: Path) -> None:
    """A gap longer than `interval x gap_factor` is blind time, not a shorter window."""
    first = list(range(_SINCE, _SINCE + 10 * _DAY, 60))
    second = list(range(_SINCE + 20 * _DAY, _UNTIL, 60))
    args = _args(
        _samples_db(tmp_path, first + second),
        _robot_db(tmp_path, [{"started_at": _SINCE, "last_seen_at": _UNTIL}]),
    )
    ac0 = _by_ac(soak._grade(args))["AC-0"]
    assert ac0.verdict == "inconclusive"
    assert ac0.rows, "the gap should be listed, not merely counted"


# ── AC-2 / AC-3: uptime and restarts ─────────────────────────────────────────────────────────


def test_a_clean_stop_is_a_manual_restart_and_fails_ac3(tmp_path: Path) -> None:
    """§12.6: a clean stop means the ordered teardown ran, which happens only because a person or
    a deploy asked. O5 is *zero* manual restarts."""
    mid = _SINCE + 10 * _DAY
    boots = [
        {
            "boot_id": "a",
            "started_at": _SINCE,
            "last_seen_at": mid,
            "stopped_at": mid,
            "stop_reason": "signal",
        },
        {"boot_id": "b", "started_at": mid + 5, "last_seen_at": _UNTIL},
    ]
    args = _args(_samples_db(tmp_path, _dense()), _robot_db(tmp_path, boots))
    graded = _by_ac(soak._grade(args))
    assert graded["AC-3"].verdict == "fail", graded["AC-3"].detail
    assert graded["AC-3b"].verdict == "pass", (
        "the still-running final record is not an unplanned stop"
    )


def test_an_unclean_stop_fails_ac3b_but_not_ac3(tmp_path: Path) -> None:
    """⚠️ The distinction O5 turns on. A crash or watchdog kill is **not** a manual restart — it is
    a separate defect, counted separately. Collapsing the two would let a crashing robot fail the
    *manual* criterion, which is a different claim about a different cause."""
    mid = _SINCE + 10 * _DAY
    boots = [
        {"boot_id": "a", "started_at": _SINCE, "last_seen_at": mid},  # stopped_at NULL
        {"boot_id": "b", "started_at": mid + 5, "last_seen_at": _UNTIL},
    ]
    args = _args(_samples_db(tmp_path, _dense()), _robot_db(tmp_path, boots))
    graded = _by_ac(soak._grade(args))
    assert graded["AC-3"].verdict == "pass", "an unclean stop is not a MANUAL restart"
    assert graded["AC-3b"].verdict == "fail", graded["AC-3b"].detail


def test_a_run_still_going_at_the_close_is_not_an_unplanned_stop(
    tmp_path: Path,
) -> None:
    """The window closing on a healthy robot must not read as a crash."""
    boots = [{"boot_id": "a", "started_at": _SINCE, "last_seen_at": _UNTIL - 30}]
    args = _args(_samples_db(tmp_path, _dense()), _robot_db(tmp_path, boots))
    graded = _by_ac(soak._grade(args))
    assert graded["AC-3b"].verdict == "pass", graded["AC-3b"].detail


def test_uptime_below_the_bar_fails(tmp_path: Path) -> None:
    """Down for a third of the window against a 99% bar."""
    boots = [{"boot_id": "a", "started_at": _SINCE, "last_seen_at": _SINCE + 20 * _DAY}]
    args = _args(_samples_db(tmp_path, _dense()), _robot_db(tmp_path, boots))
    ac2 = _by_ac(soak._grade(args))["AC-2"]
    assert ac2.verdict == "fail", ac2.detail


def test_uptime_above_the_bar_passes(tmp_path: Path) -> None:
    boots = [{"boot_id": "a", "started_at": _SINCE, "last_seen_at": _UNTIL}]
    args = _args(_samples_db(tmp_path, _dense()), _robot_db(tmp_path, boots))
    ac2 = _by_ac(soak._grade(args))["AC-2"]
    assert ac2.verdict == "pass", ac2.detail


def test_an_empty_boot_log_is_inconclusive_not_zero_uptime(tmp_path: Path) -> None:
    """⚠️ Absent is not zero. No rows means nothing to grade, which is a different statement from
    "the robot was down the whole time" — and the second would be a catastrophic misreport."""
    args = _args(_samples_db(tmp_path, _dense()), _robot_db(tmp_path, []))
    ac2 = _by_ac(soak._grade(args))["AC-2"]
    assert ac2.verdict == "inconclusive", ac2.detail


# ── every criterion reports before any verdict ───────────────────────────────────────────────


def test_an_empty_boot_log_does_not_hide_the_criteria_already_computed(
    tmp_path: Path,
) -> None:
    """⚠️ §7.1's rule, at the place the code is shaped to break it.

    `_grade` has an early `return criteria` when `boot_log` is empty. Anything computed *before*
    that must survive it — AC-0, the build guard and the memory line all depend only on `samples`.
    A reporter that dropped them would be M5's *"returned on its first failure and hid a criterion
    that had passed"*, which is the sibling of passing on silence.
    """
    graded = _by_ac(
        soak._grade(_args(_samples_db(tmp_path, _dense()), _robot_db(tmp_path, [])))
    )
    assert {"AC-0", "AC-4", "MEM", "AC-2"} <= set(graded), sorted(graded)


def test_a_failing_criterion_does_not_suppress_a_passing_one(tmp_path: Path) -> None:
    """A window that fails uptime must still report the build guard and the memory line."""
    boots = [{"boot_id": "a", "started_at": _SINCE, "last_seen_at": _SINCE + _DAY}]
    graded = _by_ac(
        soak._grade(_args(_samples_db(tmp_path, _dense()), _robot_db(tmp_path, boots)))
    )
    assert graded["AC-2"].verdict == "fail"
    assert graded["AC-4"].verdict == "pass", (
        "a passing criterion was hidden behind a failure"
    )
    assert "MEM" in graded


# ── the verdict, and the exit code a CI or an operator reads ─────────────────────────────────


def test_report_exit_codes(tmp_path: Path) -> None:
    """`fail` and `inconclusive` are both non-zero; `recorded` alone is zero.

    ⚠️ Inconclusive counting as non-zero is the load-bearing half: a run that did not establish its
    criteria must not exit 0, or a CI step reads "the soak passed".
    """
    args = _args(_samples_db(tmp_path, []), _robot_db(tmp_path, []))
    make = soak._Criterion
    assert soak._report([make("X", "x", "pass")], args) == 0
    assert soak._report([make("X", "x", "recorded")], args) == 0
    assert soak._report([make("X", "x", "fail")], args) != 0
    assert soak._report([make("X", "x", "inconclusive")], args) != 0


def test_the_bar_and_interval_come_from_the_arguments_not_a_literal(
    tmp_path: Path,
) -> None:
    """⚠️ *"Read config, never restate it."* A grader hard-coding 99% would keep printing 99% after
    the bar moved — drift with a delay fuse, and the M5 bench paid for that once."""
    boots = [{"boot_id": "a", "started_at": _SINCE, "last_seen_at": _SINCE + 25 * _DAY}]
    samples = _samples_db(tmp_path, _dense())
    robot = _robot_db(tmp_path, boots)

    strict = _by_ac(soak._grade(_args(samples, robot, bar=0.99)))["AC-2"]
    lax = _by_ac(soak._grade(_args(samples, robot, bar=0.50)))["AC-2"]

    assert strict.verdict == "fail" and lax.verdict == "pass"
    assert "99" in strict.name and "50" in lax.name, (
        f"the bar is not reported from the argument: {strict.name!r} / {lax.name!r}"
    )


def test_the_heartbeat_caveat_is_read_from_config(tmp_path: Path) -> None:
    """The uptime row states the bound its own figure is known to. That number must come from the
    loaded config, not from a literal beside it."""
    from avid.core.config import load_config

    expected = load_config(_SIM_TOML).runtime.heartbeat_interval_s
    boots = [{"boot_id": "a", "started_at": _SINCE, "last_seen_at": _UNTIL}]
    ac2 = _by_ac(
        soak._grade(_args(_samples_db(tmp_path, _dense()), _robot_db(tmp_path, boots)))
    )["AC-2"]
    assert f"{expected:.0f}s" in " ".join(ac2.rows), ac2.rows


@pytest.mark.parametrize("days", [1, 7, 30])
def test_the_window_is_the_since_until_span_not_the_sample_span(
    tmp_path: Path, days: int
) -> None:
    """Uptime is graded over the *claimed* window. A robot up for one day inside a thirty-day
    window is not at 100% — computing the ratio over the samples' own span would say it was."""
    until = _SINCE + days * _DAY
    boots = [{"boot_id": "a", "started_at": _SINCE, "last_seen_at": _SINCE + _DAY}]
    args = _args(
        _samples_db(tmp_path, list(range(_SINCE, until, 600))),
        _robot_db(tmp_path, boots),
        until=until,
    )
    ac2 = _by_ac(soak._grade(args))["AC-2"]
    assert (ac2.verdict == "pass") == (days == 1), f"{days}d: {ac2.detail}"
