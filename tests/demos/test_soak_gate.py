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
    tmp_path: Path,
    ats: list[int],
    *,
    builds: list[str] | None = None,
    uptimes: list[int | None] | None = None,
) -> str:
    """Write samples in LIST ORDER, which is what makes rowid meaningful here.

    ``ats`` is inserted in the order given, so passing a non-ascending list produces exactly the
    real defect #439 found: a rowid order that disagrees with the timestamps. ``uptimes`` carries
    the robot's own counter, ``None`` included, because an absent reading is a third answer and
    not a zero (#380).
    """
    path = tmp_path / "samples.db"
    conn = soak._open_samples(path)
    with conn:
        for i, at in enumerate(ats):
            conn.execute(
                "INSERT INTO samples (at, reachable, build, uptime_s, dropped) "
                "VALUES (?,1,?,?,0)",
                (
                    at,
                    (builds[i] if builds else _BUILD),
                    (uptimes[i] if uptimes else 60),
                ),
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
        interventions=str(Path(samples).parent / "interventions.jsonl"),
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
    assert {"AC-0", "CLOCK", "AC-4", "MEM", "AC-2"} <= set(graded), sorted(graded)


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


# ── the intervention log (#389) — §12.6 promised it and nothing implemented it ────────────────


def _interventions(tmp_path: Path, lines: list[str]) -> None:
    (tmp_path / "interventions.jsonl").write_text("\n".join(lines), encoding="utf-8")


def _unclean_boots(end: int) -> list[dict[str, Any]]:
    return [
        {"boot_id": "aaaaaaaa-1", "started_at": _SINCE, "last_seen_at": end},
        {"boot_id": "bbbbbbbb-2", "started_at": end + 30, "last_seen_at": _UNTIL},
    ]


def test_an_intervention_explains_an_unclean_stop_without_excusing_it(
    tmp_path: Path,
) -> None:
    """⚠️ The property that keeps this honest.

    A power cut and a crash leave byte-identical records — both `stopped_at` NULL, and journald is
    volatile (#381) so the kernel log is gone too. The operator's note is the only thing that can
    tell them apart, and §12.6 says so.

    But a human typing *"that one was me"* is **not a measurement**, so it must not turn AC-3b
    green. §7.1: widening to fit is a last resort *with the diagnosis attached*, and the original
    verdict stays visible. This asserts both halves — the stop is marked explained, **and** AC-3b
    still fails.
    """
    end = _SINCE + 10 * _DAY
    _interventions(
        tmp_path,
        [
            '{"at": %d, "kind": "power_cut", "note": "unplugged the bench strip"}'
            % (end + 60)
        ],
    )
    graded = _by_ac(
        soak._grade(
            _args(
                _samples_db(tmp_path, _dense()),
                _robot_db(tmp_path, _unclean_boots(end)),
            )
        )
    )
    assert graded["INTV"].verdict == "recorded", "an operator note must never grade"
    assert "1 of 1 unclean stop(s) have a matching note" in graded["INTV"].detail
    assert graded["AC-3b"].verdict == "fail", (
        "a note explained the stop and must NOT have excused it — AC-3b keeps its verdict"
    )


def test_an_unclean_stop_with_no_note_is_marked_unexplained(tmp_path: Path) -> None:
    """The distinction the log exists to create. Without it every stop looks the same."""
    end = _SINCE + 10 * _DAY
    _interventions(tmp_path, [])
    graded = _by_ac(
        soak._grade(
            _args(
                _samples_db(tmp_path, _dense()),
                _robot_db(tmp_path, _unclean_boots(end)),
            )
        )
    )
    assert any("UNEXPLAINED" in r for r in graded["INTV"].rows), graded["INTV"].rows


def test_an_absent_log_says_so_rather_than_implying_nobody_touched_it(
    tmp_path: Path,
) -> None:
    """⚠️ Absent is not zero, applied to a hand-written record.

    An empty log and an unlogged power cut are indistinguishable, and the report must say that
    rather than presenting silence as a clean bill of health."""
    graded = _by_ac(
        soak._grade(
            _args(
                _samples_db(tmp_path, _dense()),
                _robot_db(tmp_path, _unclean_boots(_SINCE + _DAY)),
            )
        )
    )
    assert graded["INTV"].verdict == "recorded"
    assert "not the same as" in graded["INTV"].detail


def test_a_malformed_line_is_counted_not_fatal(tmp_path: Path) -> None:
    """An operator's typo at 2 a.m. must not take down a thirty-day report."""
    _interventions(
        tmp_path,
        ['{"at": 1, "kind": "ok"}', "this is not json", '{"no_at_field": true}'],
    )
    graded = _by_ac(
        soak._grade(
            _args(
                _samples_db(tmp_path, _dense()),
                _robot_db(tmp_path, _unclean_boots(_SINCE + _DAY)),
            )
        )
    )
    assert graded["INTV"].verdict == "recorded"
    assert any("unreadable" in r for r in graded["INTV"].rows), graded["INTV"].rows


def test_interventions_outside_the_window_are_ignored(tmp_path: Path) -> None:
    """A note from last month's window is not evidence about this one."""
    _interventions(
        tmp_path,
        [
            '{"at": %d, "kind": "power_cut", "note": "previous window"}'
            % (_SINCE - _DAY)
        ],
    )
    graded = _by_ac(
        soak._grade(
            _args(
                _samples_db(tmp_path, _dense()),
                _robot_db(tmp_path, _unclean_boots(_SINCE + _DAY)),
            )
        )
    )
    assert "no interventions logged" in graded["INTV"].detail


# ── the clock the record is written in (#439) ────────────────────────────────────────────────
#
# The defect these guard: this Pi has no RTC, so every boot restores a stale wall clock and NTP
# steps it later. Ordered by ROWID, two consecutive samples in the live M11 window read 13:41:08
# then 13:41:07, and the earlier-WRITTEN row carried the old robot process's uptime still
# counting. Every figure the harness prints subtracts those timestamps.
#
# The shape that makes this hard to test is the same shape that made it hard to see: `_grade`'s
# own read is `ORDER BY at`, which sorts the evidence away, and `WHERE at ...`, which discards it.


def _stepped(step_at: int = 100) -> list[int]:
    """A dense window with one backwards step at index ``step_at`` — the real 13:41:08→13:41:07."""
    ats = _dense()
    ats[step_at] = ats[step_at - 1] - 1
    return ats


def test_a_backwards_step_in_write_order_is_detected_though_the_read_is_ordered_by_at(
    tmp_path: Path,
) -> None:
    """The detector must not inherit `_grade`'s ordering, or it cannot see the thing it exists for.

    `SELECT ... ORDER BY at` sorts a backwards step back into ascending order: the defect
    disappears in the act of reading it, which is why nothing noticed for a day.
    """
    ats = _stepped()
    graded = _by_ac(
        soak._grade(
            _args(
                _samples_db(tmp_path, ats),
                _robot_db(tmp_path, [{"started_at": _SINCE, "last_seen_at": _UNTIL}]),
            )
        )
    )
    clock = graded["CLOCK"]
    assert clock.verdict == "recorded", clock.detail
    assert "1 backwards step" in clock.detail, clock.detail
    assert "2 clock frames" in clock.detail, clock.detail
    assert any("step at rowid" in row for row in clock.rows), clock.rows


def test_the_clock_criterion_never_grades_and_never_moves_the_exit_code(
    tmp_path: Path,
) -> None:
    """⚠️ The whole decision, as a test: detect and say so, never grade.

    Inventing a bar here would fail a running thirty-day window for a property nobody agreed to
    grade — and `_report` turns both `fail` and `inconclusive` into a non-zero exit, so `recorded`
    is the only verdict that is genuinely inert.
    """
    args = _args(
        _samples_db(tmp_path, _stepped()),
        _robot_db(tmp_path, [{"started_at": _SINCE, "last_seen_at": _UNTIL}]),
    )
    criteria = soak._grade(args)
    assert _by_ac(criteria)["CLOCK"].verdict == "recorded"
    assert soak._report(criteria, args) == 0, "a reported line moved the exit code"


def test_a_sample_whose_stale_clock_falls_outside_the_window_is_still_read_by_write_order(
    tmp_path: Path,
) -> None:
    """⚠️ The blind spot inside the blind spot.

    `_grade` filters `WHERE at >= ? AND at < ?` — by the very clock under audit. A row stamped
    118 days early (which is what this board actually did) is dropped before any criterion sees
    it, so the evidence of the step is discarded by the query that would have reported it.
    """
    ats = _dense()
    ats[100] = _SINCE - 118 * _DAY
    clock = _by_ac(
        soak._grade(
            _args(
                _samples_db(tmp_path, ats),
                _robot_db(tmp_path, [{"started_at": _SINCE, "last_seen_at": _UNTIL}]),
            )
        )
    )["CLOCK"]
    assert "outside the graded" in clock.detail, clock.detail
    assert "1 of them outside" in clock.detail, clock.detail
    assert any(str(_SINCE - 118 * _DAY) in row for row in clock.rows), clock.rows


def test_a_clock_step_with_the_robot_uptime_falling_is_reported_as_a_new_robot_process(
    tmp_path: Path,
) -> None:
    """`uptime_s` is the robot's own counter, so it survives a clock step and can date one.

    A fall across the step means a new process began — which is how #439 established that the
    machine, not just the clock, had moved.
    """
    ats = _stepped()
    uptimes: list[int | None] = [60] * len(ats)
    uptimes[99], uptimes[100] = 1432, 47
    clock = _by_ac(
        soak._grade(
            _args(
                _samples_db(tmp_path, ats, uptimes=uptimes),
                _robot_db(tmp_path, [{"started_at": _SINCE, "last_seen_at": _UNTIL}]),
            )
        )
    )["CLOCK"]
    assert any("NEW ROBOT PROCESS" in row for row in clock.rows), clock.rows


def test_a_clock_step_while_the_robot_uptime_keeps_rising_is_not_blamed_on_a_restart(
    tmp_path: Path,
) -> None:
    """The other half, and the one that keeps the first honest.

    A clock can be corrected *underneath a running robot* — that is a timesyncd step, not a
    reboot. A detector that called every step a restart would be reporting a conclusion it never
    tested.
    """
    ats = _stepped()
    uptimes: list[int | None] = [60] * len(ats)
    uptimes[99], uptimes[100] = 1432, 1492
    clock = _by_ac(
        soak._grade(
            _args(
                _samples_db(tmp_path, ats, uptimes=uptimes),
                _robot_db(tmp_path, [{"started_at": _SINCE, "last_seen_at": _UNTIL}]),
            )
        )
    )["CLOCK"]
    assert not any("NEW ROBOT PROCESS" in row for row in clock.rows), clock.rows
    assert any("SURVIVED" in row for row in clock.rows), clock.rows


def test_an_absent_uptime_beside_a_clock_step_says_unknown_rather_than_no_restart(
    tmp_path: Path,
) -> None:
    """⚠️ #380's rule applied to the reader: absent is not zero.

    An unreachable sample reports no uptime. `int(row["uptime_s"] or 0)` would turn that into 0,
    which is less than the previous reading, and the detector would announce a restart it has no
    evidence for — a `0` from an instrument that never ran reading exactly like a real zero.
    """
    ats = _stepped()
    uptimes: list[int | None] = [60] * len(ats)
    uptimes[99], uptimes[100] = 1492, None
    clock = _by_ac(
        soak._grade(
            _args(
                _samples_db(tmp_path, ats, uptimes=uptimes),
                _robot_db(tmp_path, [{"started_at": _SINCE, "last_seen_at": _UNTIL}]),
            )
        )
    )["CLOCK"]
    assert any("CANNOT TELL" in row for row in clock.rows), clock.rows
    assert not any("NEW ROBOT PROCESS" in row for row in clock.rows), clock.rows


def test_a_started_mono_that_went_backwards_is_reported_as_a_machine_reboot(
    tmp_path: Path,
) -> None:
    """`started_mono` is the one column a clock step cannot touch — written since #379, read by
    nothing until now.

    Monotonic time only grows within one machine boot, so a decrease across consecutive runs is
    *proof* of a new origin. In the real incident that was 48.7 s against the previous run's
    15726 s, and it is what distinguished a reboot from a service restart.
    """
    boots = [
        {
            "boot_id": "e5e1d1ee",
            "started_at": _SINCE,
            "last_seen_at": _SINCE + _DAY,
            "started_mono": 15_726 * 10**9,
        },
        {
            "boot_id": "b566ffd9",
            "started_at": _SINCE + _DAY,
            "last_seen_at": _UNTIL,
            "started_mono": int(48.7 * 10**9),
        },
    ]
    clock = _by_ac(
        soak._grade(_args(_samples_db(tmp_path, _dense()), _robot_db(tmp_path, boots)))
    )["CLOCK"]
    assert any("MACHINE REBOOTED" in row for row in clock.rows), clock.rows
    assert any("48.7" in row and "15726.0" in row for row in clock.rows), clock.rows


def test_a_rising_started_mono_is_reported_as_weak_evidence_not_as_proof_of_no_reboot(
    tmp_path: Path,
) -> None:
    """⚠️ The converse does not hold, and saying it does would be the worse error.

    A reboot whose successor started later on the new monotonic clock than its predecessor did on
    the old one is indistinguishable from a service restart. "No reboot proven" is the claim;
    "the machine did not reboot" is not.
    """
    boots = [
        {
            "boot_id": "aaaa",
            "started_at": _SINCE,
            "last_seen_at": _SINCE + _DAY,
            "started_mono": 100 * 10**9,
        },
        {
            "boot_id": "bbbb",
            "started_at": _SINCE + _DAY,
            "last_seen_at": _UNTIL,
            "started_mono": 200 * 10**9,
        },
    ]
    clock = _by_ac(
        soak._grade(_args(_samples_db(tmp_path, _dense()), _robot_db(tmp_path, boots)))
    )["CLOCK"]
    assert not any("REBOOTED" in row for row in clock.rows), clock.rows


def test_ac0_and_ac2_carry_the_clock_caveat_without_changing_their_verdicts(
    tmp_path: Path,
) -> None:
    """The caveat annotates; it must never grade.

    AC-0 and AC-2 keep their own verdicts — the clock is a statement about how to *read* those
    numbers, not a new bar they have to clear.
    """
    graded = _by_ac(
        soak._grade(
            _args(
                _samples_db(tmp_path, _stepped()),
                _robot_db(tmp_path, [{"started_at": _SINCE, "last_seen_at": _UNTIL}]),
            )
        )
    )
    assert graded["AC-0"].verdict == "pass", graded["AC-0"].detail
    assert graded["AC-2"].verdict == "pass", graded["AC-2"].detail
    assert any("see CLOCK" in row for row in graded["AC-0"].rows), graded["AC-0"].rows
    assert any("see CLOCK" in row for row in graded["AC-2"].rows), graded["AC-2"].rows
    assert "heartbeat" in graded["AC-2"].rows[0], "the heartbeat caveat must stay first"


def test_a_window_with_one_clock_frame_carries_no_caveat_rows(tmp_path: Path) -> None:
    """⚠️ The passes-on-silence guard, and the reason the other nine mean anything.

    A caveat appended unconditionally would decorate every clean run, and a warning that is always
    there is a warning nobody reads. This proves the detector *discriminates*.
    """
    graded = _by_ac(
        soak._grade(
            _args(
                _samples_db(tmp_path, _dense()),
                _robot_db(tmp_path, [{"started_at": _SINCE, "last_seen_at": _UNTIL}]),
            )
        )
    )
    assert not any("see CLOCK" in row for row in graded["AC-0"].rows), graded[
        "AC-0"
    ].rows
    assert not any("see CLOCK" in row for row in graded["AC-2"].rows), graded[
        "AC-2"
    ].rows
    assert "ONE clock frame" in graded["CLOCK"].detail, graded["CLOCK"].detail


def test_the_clock_line_is_reported_even_when_no_sample_lands_in_the_window(
    tmp_path: Path,
) -> None:
    """⚠️ The branch where the clock line is most likely to be the explanation.

    "No samples between X and Y" is exactly what a clock stale by months looks like through a
    `WHERE at` filter. `_grade` returns early there, and a clock line appended after that return
    would be invisible in the one case that needs it most — §7.1's *"every criterion reports
    before any verdict is decided"*, at the place the code is shaped to break it.
    """
    stale = [at - 118 * _DAY for at in _dense()]
    graded = _by_ac(
        soak._grade(
            _args(
                _samples_db(tmp_path, stale),
                _robot_db(tmp_path, [{"started_at": _SINCE, "last_seen_at": _UNTIL}]),
            )
        )
    )
    assert graded["AC-0"].verdict == "inconclusive", graded["AC-0"].detail
    assert "CLOCK" in graded, sorted(graded)
    assert graded["CLOCK"].verdict == "recorded"


def test_an_unreadable_boot_log_cannot_stop_the_clock_line_from_reporting(
    tmp_path: Path,
) -> None:
    """A report *about* the instrument must not die with the instrument.

    ⚠️ **The weakest of these twelve, and worth saying so.** Its neuter — removing the
    `except sqlite3.Error` — makes the call *raise*, so the proof is a test error rather than a
    failing assertion. That is a weaker signal than the others and it is recorded here rather than
    dressed up.
    """
    assert soak._boot_mono_starts(str(tmp_path / "does-not-exist.db")) == []


# --- #439 AC-5: the count is what a reader acts on, so it must survive the row cap ---------------


def test_more_unplanned_stops_than_the_row_cap_still_report_the_whole_count(
    tmp_path: Path,
) -> None:
    """⚠️ The decision makes the count load-bearing, so the report must not quietly drop part of it.

    #439 AC-5 decided that a second unplanned stop does **not** end the window: AC-3b keeps
    reporting the count and O5 is still decided by uptime and manual restarts. That makes the
    *number* the signal — the verdict is binary, so one stop and thirty read `fail` alike.

    The rows are capped at ten. Without a truncation line an eleventh stop is simply absent from
    the report, which is this harness's own rule — *report the quantity you grade* — broken from
    the inside, in a gate whose whole subject is measurement honesty.
    """
    stops = 13
    boots = [
        {
            "boot_id": f"boot-{i:02d}",
            "started_at": _SINCE + i * _DAY,
            "last_seen_at": _SINCE + i * _DAY + 3600,
        }
        for i in range(stops)
    ]
    # A final, still-running record so the last row is not carved out as "still running".
    boots.append(
        {
            "boot_id": "still-running",
            "started_at": _SINCE + stops * _DAY,
            "last_seen_at": _UNTIL,
        }
    )
    ac3b = _by_ac(
        soak._grade(_args(_samples_db(tmp_path, _dense()), _robot_db(tmp_path, boots)))
    )["AC-3b"]

    assert ac3b.verdict == "fail"
    assert f"{stops} run(s)" in ac3b.detail, ac3b.detail
    assert any("more not listed" in row for row in ac3b.rows), (
        f"{stops} unplanned stops but the rows stop at {len(ac3b.rows)} with no indication that "
        "anything was dropped"
    )
