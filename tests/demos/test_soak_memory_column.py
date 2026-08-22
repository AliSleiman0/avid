"""The soak sampler's memory column and its report line (#404, SDS §12.6.1).

⚠️ **Scope, stated so it is not mistaken for coverage.** ``docs/demos/soak_pi.py`` has no gate test
of its own — M3, M4, M5, M7 and M9 each have one and M11 does not. This file tests **only what
#404 adds**: the schema migration, the promotion out of the `/metrics` body, absent-stays-absent,
and the report line's verdict. The uptime maths, the gap detection and the build-change split
remain untested, and that gap is tracked separately. Do not read a green run here as the harness
being proven.

What is tested is chosen for the same reason the rest of this project's gate tests are: each case
is a way the thirty-day window could end up holding nothing, discovered on day thirty.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

_SOAK_PATH = Path(__file__).resolve().parents[2] / "docs" / "demos" / "soak_pi.py"


def _load_soak() -> Any:
    """Import ``soak_pi`` by path — ``docs/demos`` is not a package."""
    spec = importlib.util.spec_from_file_location("soak_pi", _SOAK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["soak_pi"] = module
    spec.loader.exec_module(module)
    return module


soak = _load_soak()


# The schema as it shipped at #383, before the memory columns. Kept verbatim so the migration is
# tested against the thing it will actually meet: a database created by the previous release.
_OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id        INTEGER PRIMARY KEY,
    at        INTEGER NOT NULL,
    reachable INTEGER NOT NULL,
    build     TEXT,
    uptime_s  INTEGER,
    dropped   INTEGER,
    payload   TEXT
);
"""


# ── the migration ────────────────────────────────────────────────────────────────────────────


def test_a_database_from_the_old_schema_gains_the_columns(tmp_path: Path) -> None:
    """⚠️ The trap this migration exists for.

    ``CREATE TABLE IF NOT EXISTS`` does **nothing** to a table that already exists, so adding
    columns to ``_SCHEMA`` alone leaves any pre-existing ``samples.db`` untouched — and the failure
    surfaces at the first INSERT, as ``no such column``, potentially days into a window that cannot
    be re-run.
    """
    path = tmp_path / "samples.db"
    old = sqlite3.connect(str(path))
    old.executescript(_OLD_SCHEMA)
    old.execute(
        "INSERT INTO samples (at, reachable, build, uptime_s, dropped, payload) "
        "VALUES (1, 1, 'abc123', 60, 0, '{}')"
    )
    old.commit()
    old.close()

    conn = soak._open_samples(path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(samples)")}
        assert {"rss_bytes", "mem_available_bytes"} <= columns
        # The row that was already there survives, with NULLs for what nobody measured then.
        row = conn.execute("SELECT * FROM samples").fetchone()
        assert row["build"] == "abc123"
        assert row["rss_bytes"] is None
    finally:
        conn.close()


def test_opening_twice_is_idempotent(tmp_path: Path) -> None:
    """The sampler is ``Restart=always``; ``_open_samples`` runs on every start.

    A migration that raised ``duplicate column name`` on the second open would put the observer
    into a restart loop — an instrument that kills itself, which the unit file's own comment calls
    worse than one that misses a sample.
    """
    path = tmp_path / "samples.db"
    soak._open_samples(path).close()
    conn = soak._open_samples(path)
    conn.close()


# ── promotion out of the payload ─────────────────────────────────────────────────────────────


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


def _serve(monkeypatch: pytest.MonkeyPatch, metrics: dict[str, Any]) -> None:
    """Point the sampler's urlopen at a canned /health + /metrics."""
    import json

    def fake_urlopen(url: str, timeout: float = 0) -> _FakeResponse:
        if url.endswith("/health"):
            return _FakeResponse(b"ok")
        return _FakeResponse(json.dumps({"metrics": metrics, "absent": []}).encode())

    monkeypatch.setattr(soak.urllib.request, "urlopen", fake_urlopen)


def test_memory_is_promoted_into_its_own_columns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The same treatment ``build``/``uptime_s``/``dropped`` already get.

    The whole body stays in ``payload`` too — the columns exist so a grader over 43,200 rows does
    not have to JSON-parse each one.
    """
    _serve(monkeypatch, {"rss_bytes": 293601280, "mem_available_bytes": 1611661312})
    conn = soak._open_samples(tmp_path / "samples.db")
    try:
        soak._sample_once(conn, "http://x", 1.0)
        row = conn.execute("SELECT * FROM samples").fetchone()
        assert row["rss_bytes"] == 293601280
        assert row["mem_available_bytes"] == 1611661312
        assert "rss_bytes" in row["payload"]
    finally:
        conn.close()


def test_a_robot_that_does_not_report_memory_records_null_not_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """⚠️ #380's rule applied to the reader as well as the writer.

    A build without #404, or one whose provider landed in ``absent``, must produce NULL. A 0 here
    would be indistinguishable from a real reading and would drag any average toward a number that
    describes nothing.
    """
    _serve(monkeypatch, {"build": "abc123", "uptime_s": 60})
    conn = soak._open_samples(tmp_path / "samples.db")
    try:
        soak._sample_once(conn, "http://x", 1.0)
        row = conn.execute("SELECT * FROM samples").fetchone()
        assert row["rss_bytes"] is None
        assert row["mem_available_bytes"] is None
    finally:
        conn.close()


def test_an_unreachable_robot_records_null_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A robot that is down is the measurement — and it has no memory reading, not a zero one."""

    def refuse(url: str, timeout: float = 0) -> _FakeResponse:
        raise OSError("connection refused")

    monkeypatch.setattr(soak.urllib.request, "urlopen", refuse)
    conn = soak._open_samples(tmp_path / "samples.db")
    try:
        observation = soak._sample_once(conn, "http://x", 1.0)
        assert observation["reachable"] is False
        row = conn.execute("SELECT * FROM samples").fetchone()
        assert row["rss_bytes"] is None and row["mem_available_bytes"] is None
    finally:
        conn.close()


# ── the report line ──────────────────────────────────────────────────────────────────────────


def _rows(values: list[tuple[int | None, int | None]]) -> list[sqlite3.Row]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE s (rss_bytes INTEGER, mem_available_bytes INTEGER)")
    conn.executemany("INSERT INTO s VALUES (?,?)", values)
    return list(conn.execute("SELECT * FROM s"))


def test_the_memory_line_is_recorded_and_never_fails_the_run() -> None:
    """⚠️ The property that keeps #404 from amending O5.

    O5's criteria are uptime and zero manual restarts (SDS §12.6). Growth is *reported*. If this
    ever returned ``fail`` it would silently add a criterion to the milestone gate, and
    ``_report`` turns any failure into a non-zero exit.
    """
    mib = 1024 * 1024
    criterion = soak._memory_criterion(
        _rows([(100 * mib, 900 * mib), (400 * mib, 600 * mib)])
    )
    assert criterion.verdict == "recorded"
    assert soak._report([criterion], _args()) == 0


def test_the_memory_line_reports_n_first_last_and_delta() -> None:
    """Named honestly, with n beside it — §7.1's rule, and the reason M5's "P95" was a lie."""
    mib = 1024 * 1024
    criterion = soak._memory_criterion(
        _rows([(100 * mib, 900 * mib), (250 * mib, 700 * mib), (400 * mib, 600 * mib)])
    )
    rss_row = next(r for r in criterion.rows if "RSS" in r)
    assert "n=3" in rss_row
    assert "first 100.0 MiB" in rss_row
    assert "last 400.0 MiB" in rss_row
    assert "+300.0 MiB" in rss_row


def test_a_window_with_no_readings_says_absent_rather_than_zero() -> None:
    """⚠️ The outcome this whole issue exists to make impossible-to-miss.

    Thirty days of NULLs must read as *"the robot never reported memory"*, loudly enough that
    somebody checks the build — not as a tidy ``0.0 MiB`` that looks like a measurement.
    """
    criterion = soak._memory_criterion(_rows([(None, None), (None, None)]))
    assert criterion.verdict == "recorded"
    assert "Absent, NOT zero" in criterion.detail
    assert "0.0 MiB" not in criterion.detail


def test_one_half_absent_still_reports_the_other() -> None:
    """A criterion that reported nothing because *part* of it was missing would be the sibling of
    a reporter that returns on its first failure."""
    mib = 1024 * 1024
    criterion = soak._memory_criterion(_rows([(None, 900 * mib), (None, 600 * mib)]))
    assert any("robot RSS       absent" in r for r in criterion.rows)
    assert any("machine avail   n=2" in r for r in criterion.rows)


def _args() -> Any:
    import argparse

    return argparse.Namespace(bar=0.99, since=0, until=1)


# ── §12.6's split-window guard, finally observed failing (#388) ───────────────────────────────


def _sample_rows(builds: list[str]) -> list[sqlite3.Row]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE s (build TEXT)")
    conn.executemany("INSERT INTO s VALUES (?)", [(b,) for b in builds])
    return list(conn.execute("SELECT * FROM s"))


def _build_criterion(rows: list[sqlite3.Row]) -> Any:
    """AC-4's computation, exactly as `_grade` performs it.

    Extracted rather than driving the whole grader, which needs a config, a robot DB and a boot
    log. The expression is copied verbatim from `soak_pi.py`; if it drifts there this test keeps
    passing, which is the honest limitation of testing a fragment.
    """
    builds = sorted({str(r["build"]) for r in rows if r["build"]})
    return len(builds) <= 1, builds


def test_the_build_guard_fails_when_the_window_holds_two_builds() -> None:
    """⚠️ This guard had NEVER been observed failing, and could not be (#388).

    `build` came from `avid.__version__`, which is `version = "0.0.0"` in `pyproject.toml` — the
    same string for every commit. So `len({builds}) <= 1` was always true and §12.6's
    split-window rule detected nothing. Verified on the rig: `/metrics` reported `0.0.0` before
    and after a pull that moved HEAD five commits.

    A criterion that cannot fail is a criterion that passes on silence, and this one sits in the
    gate that decides `v1.0.0`. Now that `resolve_build_id` produces a real identifier, the guard
    can fire — and this is the test that says it does.
    """
    passed, builds = _build_criterion(
        _sample_rows(["v0.M10.0-41-gf2e8e74", "v0.M10.0-43-gaaaaaaa"])
    )
    assert not passed, f"two builds in one window graded as one: {builds}"
    assert len(builds) == 2


def test_the_build_guard_passes_on_a_single_build() -> None:
    """The other end. A guard that convicted everything would satisfy the test above alone."""
    passed, builds = _build_criterion(_sample_rows(["v0.M10.0-41-gf2e8e74"] * 5))
    assert passed
    assert builds == ["v0.M10.0-41-gf2e8e74"]


def test_a_window_of_only_the_old_static_version_still_grades_as_one_build() -> None:
    """⚠️ The regression this must never quietly become again.

    If the identifier ever reverts to a constant, the guard silently returns to being inert — it
    keeps *passing*, which is exactly why nobody noticed for three milestones. Nothing here can
    detect that from inside the grader, so the detection lives with the resolver
    (`tests/adapters/test_build_id.py::test_two_commits_produce_two_different_identifiers`).
    This case exists to record the coupling, so a future reader knows where the real guard is.
    """
    passed, builds = _build_criterion(_sample_rows(["0.0.0"] * 40))
    assert passed and builds == ["0.0.0"]
