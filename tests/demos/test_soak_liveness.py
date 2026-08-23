"""The soak's liveness criterion — the one it was missing when it graded a wedged robot (#452 AC-4).

The M11 window ran ~26 hours over a robot that entered `THINKING` at minute 3 and never left, and
**every graded criterion passed**: AC-2 read 99.89% uptime, RSS was flat, the watchdog was fed,
zero events were dropped. Each of those measures the *process*, and each is satisfied by a robot
doing nothing at all. This file is the test suite for the criterion that can tell the difference.

⚠️ **Scope, stated so it is not mistaken for coverage.** These cases cover what #452 AC-4 adds: the
two new columns and their migration, the promotion out of `/state` and `/metrics`, and the
criterion's verdicts. `test_soak_gate.py` owns the uptime maths, the gap detection and the clock
family; the harness as a whole is still not proven by a green run here.

Every case below is a way this criterion could be worse than nothing:

* it could **miss the wedge** it exists for;
* it could **fail a healthy robot** — for cycling between samples, for a sampler outage, for a
  restart, or for spending a legitimate night in SLEEPING — which is how a gate becomes something
  people rerun without reading;
* it could **pass on silence**, reporting a clean liveness figure from a build that never answered
  `/state` at all. That is this project's most expensive recurring defect: a `0` from an absent
  instrument reads exactly like a real `0`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SIM_TOML = str(_ROOT / "config" / "sim.toml")


def _load_soak() -> Any:
    spec = importlib.util.spec_from_file_location(
        "soak_pi_liveness", _ROOT / "docs" / "demos" / "soak_pi.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["soak_pi_liveness"] = module
    spec.loader.exec_module(module)
    return module


soak = _load_soak()

_BUILD = "v0.M10.0-47-g8d03690"
_INTERVAL = 60.0
_GAP_BOUND = _INTERVAL * 3.0
# `[gate] think_timeout_s` in config/sim.toml. Named here as the *test's own* reference rather than
# read from config on purpose: a test that recomputes the value under test from the same source
# asserts only that two reads agree. `test_the_bound_is_read_from_config_not_restated` pins the
# two together, which is the assertion that actually bites if the config moves.
_THINK_BOUND = 10.0
_BOUNDS = {"THINKING": _THINK_BOUND}


# The schema as it shipped at #404 — with the memory columns, without the liveness pair. This is
# what the migration will actually meet on the rig: a database written by the previous release,
# mid-window.
_PRE_452_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id        INTEGER PRIMARY KEY,
    at        INTEGER NOT NULL,
    reachable INTEGER NOT NULL,
    build     TEXT,
    uptime_s  INTEGER,
    dropped   INTEGER,
    rss_bytes INTEGER,
    mem_available_bytes INTEGER,
    payload   TEXT
);
"""


def _rows(samples: list[dict[str, Any]]) -> list[sqlite3.Row]:
    """Build sample rows in list order, with the columns the criterion reads.

    Defaults are the healthy case, so each test states only the thing it is about — and `None` is
    always expressible, because "the robot did not report this" is a third answer that several of
    these cases turn on.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE s (at INTEGER, build TEXT, uptime_s INTEGER, state TEXT, "
        "transitions INTEGER, payload TEXT)"
    )
    for i, sample in enumerate(samples):
        # ⚠️ `payload` is here because the real table has it, not because most of these cases care.
        # It was left out at first and #456's reader — which reads the stored /metrics body rather
        # than a column — died on every one of them with `IndexError: No item with that key`. The
        # fixture that omits a column is the fixture that hides the next defect.
        rejected = sample.get("rejected")
        payload = sample.get(
            "payload",
            json.dumps(
                {
                    "metrics": {"illegal_transitions": rejected}
                    if rejected is not None
                    else {},
                    "absent": [],
                }
            ),
        )
        conn.execute(
            "INSERT INTO s VALUES (?,?,?,?,?,?)",
            (
                sample.get("at", 1_700_000_000 + i * int(_INTERVAL)),
                sample.get("build", _BUILD),
                sample.get("uptime_s", 100 + i * int(_INTERVAL)),
                sample.get("state", "IDLE"),
                sample.get("transitions", 7),
                payload,
            ),
        )
    return list(conn.execute("SELECT * FROM s"))


def _held(state: str, count: int, **over: Any) -> list[dict[str, Any]]:
    """`count` consecutive samples in `state`, with the transitions counter standing still."""
    return [{"state": state, **over} for _ in range(count)]


def _grade_liveness(samples: list[dict[str, Any]], **over: Any) -> Any:
    kwargs: dict[str, Any] = dict(
        bounds=_BOUNDS, interval=_INTERVAL, gap_bound=_GAP_BOUND
    )
    kwargs.update(over)
    return soak._liveness_criterion(_rows(samples), **kwargs)


# ── the migration ────────────────────────────────────────────────────────────────────────────


def test_a_database_from_the_pre_452_schema_gains_the_columns(tmp_path: Path) -> None:
    """⚠️ The trap `_add_missing_columns` exists for, met a second time.

    `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists. The rig's
    `samples.db` was written by the build that ran the void window, so the first INSERT after this
    deploy would die with `no such column` — days into a run that cannot be re-run.
    """
    path = tmp_path / "samples.db"
    old = sqlite3.connect(str(path))
    old.executescript(_PRE_452_SCHEMA)
    old.execute(
        "INSERT INTO samples (at, reachable, build, uptime_s, dropped, rss_bytes, payload) "
        "VALUES (1, 1, 'abc123', 60, 0, 1024, '{}')"
    )
    old.commit()
    old.close()

    conn = soak._open_samples(path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(samples)")}
        assert {"state", "transitions"} <= columns
        row = conn.execute("SELECT * FROM samples").fetchone()
        # The pre-existing row survives, carrying NULL for what nobody measured then.
        assert row["build"] == "abc123" and row["rss_bytes"] == 1024
        assert row["state"] is None and row["transitions"] is None
    finally:
        conn.close()


# ── promotion out of /state and /metrics ─────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    *,
    metrics: dict[str, Any],
    state: dict[str, Any] | None,
) -> None:
    """Canned `/health`, `/metrics` and `/state`. `state=None` serves a 404, like an old build."""

    def fake_urlopen(url: str, timeout: float = 0) -> _FakeResponse:
        if url.endswith("/health"):
            return _FakeResponse(b"ok")
        if url.endswith("/state"):
            if state is None:
                raise OSError("HTTP Error 404: Not Found")
            return _FakeResponse(json.dumps(state).encode())
        return _FakeResponse(json.dumps({"metrics": metrics, "absent": []}).encode())

    monkeypatch.setattr(soak.urllib.request, "urlopen", fake_urlopen)


def test_the_state_and_transition_count_are_promoted_into_columns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same treatment `build`/`uptime_s`/`rss_bytes` already get.

    A grader over 43,200 rows should not have to JSON-parse each one to ask what the robot was
    doing — and `payload` still holds the whole body for the questions nobody has asked yet.
    """
    _serve(
        monkeypatch,
        metrics={"build": _BUILD, "transitions": 412},
        state={"state": "SLEEPING", "affect": "CONTENT", "absent": []},
    )
    conn = soak._open_samples(tmp_path / "samples.db")
    try:
        soak._sample_once(conn, "http://x", 1.0)
        row = conn.execute("SELECT * FROM samples").fetchone()
        assert row["state"] == "SLEEPING"
        assert row["transitions"] == 412
    finally:
        conn.close()


def test_a_build_without_the_state_route_records_null_not_a_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """⚠️ #380's rule, and the one the criterion's `inconclusive` verdict rests on.

    A 404 on `/state` must not cost us `/metrics` — the two reads are independent — and it must
    record NULL. Anything else would let a build that cannot answer the question look like a robot
    that answered IDLE.
    """
    _serve(monkeypatch, metrics={"build": _BUILD, "transitions": 3}, state=None)
    conn = soak._open_samples(tmp_path / "samples.db")
    try:
        soak._sample_once(conn, "http://x", 1.0)
        row = conn.execute("SELECT * FROM samples").fetchone()
        assert row["state"] is None
        assert row["transitions"] == 3, "a 404 on /state cost us the /metrics reading"
    finally:
        conn.close()


def test_a_state_field_the_robot_could_not_read_records_null(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`/state` names an unreadable field in `absent` rather than defaulting it — so must we.

    `StateReport.snapshot` is explicit about this: *"a `/state` that reported IDLE because nothing
    was wired would be a lie with a plausible face."* The sampler inherits that or discards it.
    """
    _serve(
        monkeypatch,
        metrics={"build": _BUILD, "transitions": 3},
        state={"affect": "CONTENT", "absent": ["state"]},
    )
    conn = soak._open_samples(tmp_path / "samples.db")
    try:
        soak._sample_once(conn, "http://x", 1.0)
        assert conn.execute("SELECT * FROM samples").fetchone()["state"] is None
    finally:
        conn.close()


def test_an_unreachable_robot_records_null_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A robot that is down is the measurement — and it has no state, not a stale one."""

    def refuse(url: str, timeout: float = 0) -> _FakeResponse:
        raise OSError("connection refused")

    monkeypatch.setattr(soak.urllib.request, "urlopen", refuse)
    conn = soak._open_samples(tmp_path / "samples.db")
    try:
        observation = soak._sample_once(conn, "http://x", 1.0)
        assert observation["reachable"] is False
        row = conn.execute("SELECT * FROM samples").fetchone()
        assert row["state"] is None and row["transitions"] is None
    finally:
        conn.close()


# ── the criterion: catching the wedge ────────────────────────────────────────────────────────


def test_the_wedge_fails(tmp_path: Path) -> None:
    """#452, reproduced as the soak would have seen it. The case this criterion exists for.

    Twenty consecutive samples reading THINKING with the transitions counter frozen: twenty
    minutes at a 60 s cadence, against a 10 s bound. On the rig it was 24 hours — 8,600x the
    bound — and the window reported success.
    """
    criterion = _grade_liveness(_held("THINKING", 20))
    assert criterion.verdict == "fail"
    assert "1 breach" in criterion.detail
    assert any("THINKING held" in row for row in criterion.rows)


def test_a_wedge_is_reported_in_seconds_the_robot_can_be_held_to(
    tmp_path: Path,
) -> None:
    """The held time comes from the robot's own uptime counter, not the wall clock.

    ⚠️ This board has no RTC and #439 found its `at` column going *backwards* between consecutive
    writes. Every other figure in this harness is a subtraction between two readings of that
    clock and says so; this one does not have to be, because `uptime_s` is the robot's own count
    and only ever moves forward within a process. A criterion that can fail a run should rest on
    the most trustworthy reading available.
    """
    samples = [
        {"state": "THINKING", "uptime_s": 1000 + i * 60, "at": 1_700_000_000 + i * 60}
        for i in range(5)
    ]
    # The wall clock disagrees with itself across the run — the #439 shape, exaggerated.
    samples[3]["at"] = 1_600_000_000
    criterion = _grade_liveness(samples)
    assert criterion.verdict == "fail"
    assert any("240s" in row and "robot uptime" in row for row in criterion.rows)


def test_a_run_with_no_uptime_reading_falls_back_to_the_wall_clock_and_says_so() -> (
    None
):
    """A duration taken from an untrusted clock is still worth having — labelled as one."""
    criterion = _grade_liveness(_held("THINKING", 5, uptime_s=None))
    assert criterion.verdict == "fail"
    assert any("wall clock" in row for row in criterion.rows)


# ── the criterion: NOT failing a healthy robot ───────────────────────────────────────────────


def test_a_robot_cycling_between_samples_is_not_a_wedge() -> None:
    """⚠️ The false positive that would have made this criterion useless.

    A talking robot goes THINKING -> SPEAKING -> IDLE -> LISTENING -> THINKING between two samples
    60 s apart, so the *state readings* are identical and prove nothing on their own. The
    transitions counter is what separates the two cases, and this is the test that fails if
    anybody ever "simplifies" the criterion down to the state series alone.
    """
    samples = [{"state": "THINKING", "transitions": 100 + i * 4} for i in range(20)]
    criterion = _grade_liveness(samples)
    assert criterion.verdict == "pass"
    assert "no transient state was provably held" in criterion.detail


def test_a_long_night_in_sleeping_is_not_a_wedge() -> None:
    """SDS §12.6 is explicit: **a SLEEPING robot is UP.**

    SLEEPING is a state the design intends (M8's nap), and IDLE is where a companion spends most
    of a quiet night. Grading held time on them would penalise the feature — the same mistake as
    counting SLEEPING as downtime, one layer up.
    """
    for state in ("IDLE", "SLEEPING", "DEGRADED"):
        criterion = _grade_liveness(_held(state, 500))
        assert criterion.verdict == "pass", state


def test_no_resting_state_is_gradeable_at_all() -> None:
    """⚠️ The exclusion lives in a table, so it needs an assertion on the table.

    Every other case here passes `bounds` in directly, which means none of them would notice if
    IDLE or SLEEPING acquired an entry in `_TRANSIENT_STATES` — and the grade pass builds its
    bounds from exactly that table. This is the assertion that fails if a future edit makes a
    quiet night gradeable, which S12.6 forbids in as many words: **a SLEEPING robot is UP.**
    """
    assert set(soak._TRANSIENT_STATES) & set(soak._RESTING_STATES) == set()
    assert "SLEEPING" not in soak._TRANSIENT_STATES
    assert "IDLE" not in soak._TRANSIENT_STATES
    assert "DEGRADED" not in soak._TRANSIENT_STATES


def test_a_state_with_no_designed_bound_is_reported_and_not_graded() -> None:
    """⚠️ LISTENING has no bound because `Trigger.LISTEN_TIMEOUT` is **unwired**.

    SDS §3.10.3 documents a 30 s listen timeout, the row exists, and nothing drives it — which is
    precisely the shape #452 was. Inventing a bar here would be a bar nobody agreed to; the
    criterion reports the held time and names the gap instead.
    """
    criterion = _grade_liveness(_held("LISTENING", 60))
    assert criterion.verdict == "pass"
    assert any("longest LISTENING" in row for row in criterion.rows)
    assert any(
        "no bound exists for" in row and "LISTENING" in row for row in criterion.rows
    )


def test_a_sampler_outage_does_not_fail_the_robot() -> None:
    """⚠️ A hole in the record is the sampler's finding (AC-0), never the robot's.

    Not splitting runs at a gap would *overstate* held time across the hole and could fail a
    healthy robot for its observer's outage — the criterion convicting on the absence of evidence,
    which is the opposite of what a soak is for.
    """
    samples = [
        {"state": "THINKING", "at": 1_700_000_000, "uptime_s": 100},
        # Four hours later. Anything at all could have happened in between.
        {"state": "THINKING", "at": 1_700_014_400, "uptime_s": 14_500},
    ]
    criterion = _grade_liveness(samples)
    assert criterion.verdict == "pass"


def test_a_restart_splits_the_run() -> None:
    """A new process is a new subject, and its counter starts again from zero.

    Reading `transitions` across a restart as "unchanged" would manufacture a wedge out of two
    healthy processes that happened to be in the same state when each was sampled.

    ⚠️ The counter here **matches by coincidence across the restart**, and that is the whole
    fixture: a fresh boot walks BOOTING -> IDLE -> LISTENING -> THINKING, so "3" on either side of
    a restart is an ordinary thing to see. A version of this test with mismatched counters passes
    whether or not the restart splits anything — the run is simply unproven — which is a green
    test proving nothing.

    The assertion is on the reported figure, not the verdict, for the same reason: without the
    split the run spans two processes and its held time comes out **negative**, which sails past a
    "> bound" check and reads as a pass.
    """
    samples = [
        {"state": "THINKING", "uptime_s": 5_000, "transitions": 3},
        {"state": "THINKING", "uptime_s": 5_060, "transitions": 3},
        # Restarted: uptime falls back to a fresh process's count.
        {"state": "THINKING", "uptime_s": 30, "transitions": 3},
        {"state": "THINKING", "uptime_s": 90, "transitions": 3},
    ]
    criterion = _grade_liveness(samples)
    assert criterion.verdict == "pass", "a restart was read as one long wedge"
    assert any(
        "longest THINKING  60s over 2 samples" in row for row in criterion.rows
    ), f"the run spans the restart: {criterion.rows}"


def test_a_build_change_splits_the_run() -> None:
    """Same argument as the restart: two builds are two subjects (#373)."""
    samples = [
        {"state": "THINKING", "build": "build-a", "uptime_s": 5_000},
        {"state": "THINKING", "build": "build-a", "uptime_s": 5_060},
        {"state": "THINKING", "build": "build-b", "uptime_s": 5_120},
        {"state": "THINKING", "build": "build-b", "uptime_s": 5_180},
    ]
    assert _grade_liveness(samples).verdict == "pass"


def test_a_single_sample_in_a_state_establishes_no_duration() -> None:
    """One reading is a reading, not a duration — it says nothing about how long anything lasted."""
    samples = [{"state": "THINKING"}, {"state": "IDLE", "transitions": 8}]
    criterion = _grade_liveness(samples)
    assert criterion.verdict == "pass"
    assert not any("longest THINKING" in row for row in criterion.rows)


def test_the_slack_is_one_sampling_interval_and_it_is_printed() -> None:
    """The sampler sees the machine at its own cadence, and that uncertainty is stated, not argued.

    A state entered just after one sample and left just before the next is indistinguishable from
    one held the whole time, so every bound carries one interval of slack — and a run that lands
    inside the slack passes.
    """
    # Two samples 60 s apart: 60 s held, against a 10 s bound + 60 s slack. Inside, so it passes.
    criterion = _grade_liveness(_held("THINKING", 2))
    assert criterion.verdict == "pass"
    assert any("+60s slack" in row for row in criterion.rows)
    # Three samples: 120 s held, past 70 s. Outside.
    assert _grade_liveness(_held("THINKING", 3)).verdict == "fail"


# ── the criterion: never passing on silence ──────────────────────────────────────────────────


def test_a_window_with_no_state_series_is_inconclusive_not_a_pass() -> None:
    """⚠️ The defect this whole criterion is a response to, one level up.

    A build too old to serve `/state` produces no state column, and a criterion that read that as
    "nothing went wrong" would be the void window all over again: a clean liveness figure from an
    instrument that was never there. `0` from an absent instrument reads exactly like a real `0`.
    """
    criterion = _grade_liveness([{"state": None} for _ in range(500)])
    assert criterion.verdict == "inconclusive"
    assert "ABSENT, not idle" in criterion.detail


def test_states_without_a_transition_counter_are_inconclusive_not_a_pass() -> None:
    """The other half of the pair, absent on its own.

    With no counter, no run can be PROVEN still — identical readings 60 s apart are equally
    consistent with a wedged robot and a talking one. That is not a pass; it is a window that did
    not establish the criterion.
    """
    criterion = _grade_liveness(_held("THINKING", 20, transitions=None))
    assert criterion.verdict == "inconclusive"
    assert "ABSENT, not zero" in criterion.detail


def test_an_unproven_run_is_reported_and_cannot_fail_anything() -> None:
    """A run whose stillness is not proven is printed, and convicts nobody.

    Failing on it would be convicting on the absence of evidence — the same error as reading an
    absent counter as an unchanged one.
    """
    samples = [{"state": "THINKING", "transitions": None} for _ in range(20)]
    samples.append({"state": "IDLE", "transitions": 5})
    criterion = _grade_liveness(samples)
    assert criterion.verdict != "fail"
    assert any("NOT proven still" in row for row in criterion.rows)


# ── the reported half ────────────────────────────────────────────────────────────────────────


def test_the_transition_total_is_reported_and_never_graded() -> None:
    """⚠️ Deliberately not a bar, and the asymmetry is the point.

    A minimum transition count cannot be defended: an empty house legitimately produces none, and
    a criterion that fails because nobody came home is one people learn to rerun without reading.
    The bounded state is the half with teeth; this is the half a person reads.
    """
    criterion = _grade_liveness([{"state": "IDLE", "transitions": 7} for _ in range(5)])
    assert criterion.verdict == "pass"
    assert any("transitions     7" in row for row in criterion.rows)


def test_the_transition_total_sums_across_a_restart() -> None:
    """The counter is process-scoped, so a fall is a new process rather than a negative count."""
    rows = _rows(
        [
            {"transitions": 10},
            {"transitions": 40},
            {"transitions": 3},  # restarted
            {"transitions": 9},
        ]
    )
    assert soak._transition_total(rows) == 49


def test_the_transition_total_is_absent_not_zero_when_never_reported() -> None:
    """`None`, and printed as ABSENT. A zero here would be a claim nobody measured."""
    assert soak._transition_total(_rows([{"transitions": None}] * 5)) is None
    criterion = _grade_liveness(_held("IDLE", 5, transitions=None))
    assert any("transitions     ABSENT" in row for row in criterion.rows)


def test_the_breach_rows_are_capped_and_say_so() -> None:
    """A cap that does not announce itself reads as "that was all of them" (§7.1)."""
    samples: list[dict[str, Any]] = []
    for run in range(15):
        samples.extend(_held("THINKING", 3, transitions=1000 + run))
        samples.append({"state": "IDLE", "transitions": 1000 + run})
    criterion = _grade_liveness(samples)
    assert criterion.verdict == "fail"
    assert any("more not listed" in row for row in criterion.rows)


# ── the rejected-transition pairs, reported beside liveness (#456 AC-5) ──────────────────────


def test_the_rejected_pairs_are_read_out_of_the_stored_payload() -> None:
    """#456 needs no schema change: the sampler has kept the whole `/metrics` body since #383.

    That is the column's stated purpose — *"for questions not yet asked"* — and this is one of
    them, which means it also works retroactively on any window a build with the counter sampled.
    """
    rows = _rows(
        [
            {"rejected": {"THINKING/PRESENCE_LOST_TIMEOUT": 3}},
            {"rejected": {"THINKING/PRESENCE_LOST_TIMEOUT": 135, "IDLE/X": 1}},
        ]
    )
    assert soak._rejected_pairs(rows) == {
        "THINKING/PRESENCE_LOST_TIMEOUT": 135,
        "IDLE/X": 1,
    }


def test_the_worst_reading_wins_not_the_last_and_not_the_sum() -> None:
    """⚠️ The counter is process-scoped, so a restart resets it.

    Summing would double-count a window that restarted; taking the last would report a fresh
    process's small number over a wedge that ran for a day before it — which is the reading that
    would have made the M11 window look fine. The max is the worst thing that was ever true of one
    process, which is the question a reader is actually asking.
    """
    rows = _rows(
        [
            {"rejected": {"A/B": 10}},
            {"rejected": {"A/B": 135}},
            {"rejected": {"A/B": 2}},  # restarted; the counter began again
        ]
    )
    assert soak._rejected_pairs(rows) == {"A/B": 135}


def test_a_malformed_payload_is_a_sample_with_nothing_to_say() -> None:
    """A report *about* the instrument must not die with the instrument."""
    rows = _rows(
        [
            {"payload": None},
            {"payload": "not json at all"},
            {"payload": '{"metrics": {"illegal_transitions": "not a map"}}'},
            {"rejected": {"A/B": 4}},
        ]
    )
    assert soak._rejected_pairs(rows) == {"A/B": 4}


def test_the_wedge_names_the_pair_that_caused_it() -> None:
    """The whole point: `LIVE` says the robot was stuck, and this says what it kept refusing.

    The numbers are the rig's own, read off the machine before it was deployed over: the nap timer
    arriving at a machine parked in THINKING, 135 times, beside the one documented benign
    rejection at 1 (#224). A reader sees which is which without being told a threshold.
    """
    samples = _held("THINKING", 20)
    samples[-1]["rejected"] = {
        "THINKING/PRESENCE_LOST_TIMEOUT": 135,
        "IDLE/VISION_PRESENCE_GAINED": 1,
    }
    criterion = _grade_liveness(samples)
    assert criterion.verdict == "fail"  # LIVE still convicts, from the state side
    assert any(
        "THINKING/PRESENCE_LOST_TIMEOUT" in row and "x135" in row
        for row in criterion.rows
    )
    assert any(
        "IDLE/VISION_PRESENCE_GAINED" in row and "x1" in row for row in criterion.rows
    )


def test_no_rejected_pairs_says_which_of_the_two_things_it_means() -> None:
    """⚠️ Absent is not zero, one layer further out than usual.

    An empty reading here is ambiguous in a way the other metrics are not: it means either *the
    machine refused nothing* or *the build predates the counter*. The row says both rather than
    letting a reader take the flattering one — which is the same defect as a `0` from an
    instrument that was never there.
    """
    criterion = _grade_liveness(_held("IDLE", 3, transitions=7))
    assert any("either the machine refused nothing" in row for row in criterion.rows)


def test_the_pair_list_is_capped_and_says_so() -> None:
    """A cap that does not announce itself reads as "that was all of them" (§7.1)."""
    samples = _held("IDLE", 3)
    samples[-1]["rejected"] = {f"S{i}/T{i}": i + 1 for i in range(9)}
    criterion = _grade_liveness(samples)
    assert any(
        "more pair(s) not listed" in row and "9 is the whole figure" in row
        for row in criterion.rows
    )


# ── wired into the real grade pass ───────────────────────────────────────────────────────────


def _samples_db(tmp_path: Path, samples: list[dict[str, Any]]) -> str:
    path = tmp_path / "samples.db"
    conn = soak._open_samples(path)
    with conn:
        for i, sample in enumerate(samples):
            conn.execute(
                "INSERT INTO samples (at, reachable, build, uptime_s, dropped, state, "
                "transitions) VALUES (?,1,?,?,0,?,?)",
                (
                    sample["at"],
                    sample.get("build", _BUILD),
                    sample.get("uptime_s", 100 + i * 60),
                    sample.get("state", "IDLE"),
                    sample.get("transitions", 7),
                ),
            )
    conn.close()
    return str(path)


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


def _robot_db(tmp_path: Path, *, since: int, until: int) -> str:
    """One clean run covering the window, so the boot-log criteria have nothing to say."""
    path = tmp_path / "robot.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_BOOT_SCHEMA)
    conn.execute(
        "INSERT INTO boot_log VALUES (?,?,?,?,?,?,?)",
        ("boot-0", _BUILD, since, 1, until, None, None),
    )
    conn.commit()
    conn.close()
    return str(path)


def _args(
    tmp_path: Path, samples: str, *, since: int, until: int
) -> argparse.Namespace:
    return argparse.Namespace(
        config=_SIM_TOML,
        samples=samples,
        robot_db=_robot_db(tmp_path, since=since, until=until),
        since=since,
        until=until,
        interval=_INTERVAL,
        gap_factor=3.0,
        min_coverage=0.99,
        bar=0.99,
        interventions=str(tmp_path / "interventions.jsonl"),
    )


def test_the_grade_pass_reports_liveness_and_a_wedge_moves_the_exit_code(
    tmp_path: Path,
) -> None:
    """⚠️ The wiring, driven through the real entry point rather than the criterion function.

    Testing a criterion in isolation does not test the pass that runs it — M7's harness had
    sixteen green tests over functions the dispatcher had stopped calling correctly. This drives
    `_grade` and `_report` and asserts three things at once: the criterion appears, a wedge
    **fails** the run, and the robot's other criteria still report above the verdict.
    """
    since = 1_700_000_000
    # Long enough that the one interval of lead-out at the window's edge does not cost AC-0 its
    # own coverage bar — this test is about LIVE, and an unrelated INCONCL would prove nothing.
    until = since + 200 * 60
    wedged = [
        {"at": since + i * 60, "state": "THINKING", "uptime_s": 100 + i * 60}
        for i in range(200)
    ]
    args = _args(tmp_path, _samples_db(tmp_path, wedged), since=since, until=until)

    criteria = soak._grade(args)
    by_ac = {c.ac: c for c in criteria}
    assert "LIVE" in by_ac, "the grade pass does not run the liveness criterion at all"
    assert by_ac["LIVE"].verdict == "fail"
    assert soak._report(criteria, args) != 0

    # ...and the criteria that had nothing to do with it still reported. A failing figure must not
    # hide a passing one (§7.1).
    assert by_ac["AC-0"].verdict == "pass"
    assert by_ac["AC-4"].verdict == "pass"
    assert "MEM" in by_ac


def test_the_bound_is_read_from_config_not_restated(tmp_path: Path) -> None:
    """⚠️ Config is read, never restated — a literal in a gate is drift with a delay fuse (§7.1).

    This is the assertion that bites if `[gate] think_timeout_s` ever moves: the criterion's
    printed bound must equal the value the loaded config carries, not the number someone typed
    into this harness. The test's own reference constant is spelled out at the top of the file
    rather than recomputed from config, because a test that reads its expectation from the source
    under test asserts only that two reads agree.
    """
    from avid.core.config import load_config

    assert load_config(_SIM_TOML).gate.think_timeout_s == _THINK_BOUND

    since = 1_700_000_000
    until = since + 600
    healthy = [
        {"at": since + i * 60, "state": "IDLE", "transitions": 7 + i} for i in range(10)
    ]
    args = _args(tmp_path, _samples_db(tmp_path, healthy), since=since, until=until)
    live = {c.ac: c for c in soak._grade(args)}["LIVE"]
    assert any(f"'THINKING': {_THINK_BOUND}" in row for row in live.rows)
