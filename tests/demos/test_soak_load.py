"""The soak load generator's own gate (#389, SDS §12.6).

The generator exists so a 72-hour window measures a working robot instead of an idle one. Every
case below is a way it could be **worse than nothing**:

* it could **overspend** — and §10.4's policy gate does not throttle reactive turns, so the caps in
  this file are the only caps that exist anywhere;
* it could **stop silently**, leaving a window that looks idle and a report that cannot say why;
* it could **crash** on a refused device or an unreachable robot, which is the same outcome;
* it could **play over the robot's own reply**, where the clip is discarded against the echo floor
  and the money is spent for nothing;
* or it could pass a corpus that will never trip the gate, which produces a window measuring
  exactly the thing the generator was built to prevent.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import wave
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SIM_TOML = str(_ROOT / "config" / "sim.toml")


def _load_generator() -> Any:
    """Import ``soak_load`` by path — ``docs/demos`` is not a package.

    The ``sys.modules`` registration precedes ``exec_module`` because ``@dataclass`` resolves
    ``__module__`` through it while the class body runs.
    """
    spec = importlib.util.spec_from_file_location(
        "soak_load", _ROOT / "docs" / "demos" / "soak_load.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["soak_load"] = module
    spec.loader.exec_module(module)
    return module


load = _load_generator()


# ── the meter: process-scoped counters across a restart ──────────────────────────────────────


def test_the_meter_accumulates_rises_not_readings() -> None:
    """An ordinary run: the counters only ever go up, so the total is the last reading."""
    meter = load._Meter()
    for turns, usd in ((3, 0.10), (7, 0.30), (11, 0.55)):
        meter.observe(turns, usd)
    assert meter.turns == 11
    assert meter.usd == pytest.approx(0.55)


def test_the_meter_survives_a_restart_without_forgetting_or_double_counting() -> None:
    """⚠️ The trap this exists for, and it is the same one #456 handles for transitions.

    `/metrics` counters are **process-scoped**, and `Restart=always` guarantees the robot restarts
    during a long window. Taking the last reading would forget everything before the restart and
    let the run spend its whole budget again; summing every reading would double-count and stop the
    run early. Only the rises are new work.
    """
    meter = load._Meter()
    meter.observe(40, 1.80)
    meter.observe(50, 2.25)
    meter.observe(2, 0.09)  # restarted: the counter began again
    meter.observe(6, 0.27)

    assert meter.turns == 56, "a restart lost the earlier turns or replayed them"
    assert meter.usd == pytest.approx(2.52)


def test_an_absent_counter_moves_nothing() -> None:
    """A robot that did not report is not a robot that did nothing (#380)."""
    meter = load._Meter()
    meter.observe(5, 0.20)
    meter.observe(None, None)
    assert meter.turns == 5
    assert meter.usd == pytest.approx(0.20)


# ── the corpus validator ─────────────────────────────────────────────────────────────────────


def _wav(path: Path, *, seconds: float, amplitude: int, rate: int = 16000) -> Path:
    """A crude tone at a chosen level — enough to exercise the level and duration checks."""
    import math
    import struct

    frames = int(rate * seconds)
    pcm = b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 220 * i / rate)))
        for i in range(frames)
    )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return path


def _corpus(tmp_path: Path, entries: list[dict[str, Any]]) -> Path:
    corpus = tmp_path / "load"
    corpus.mkdir()
    (corpus / "manifest.json").write_text(
        json.dumps({"utterances": entries}), encoding="utf-8"
    )
    return corpus


def _validate_args(corpus: Path) -> argparse.Namespace:
    return argparse.Namespace(config=_SIM_TOML, corpus=str(corpus))


def test_a_clip_too_quiet_to_clear_a_room_is_rejected(tmp_path: Path) -> None:
    """The floor is on the file, and it is the cheap half of the question.

    A clip at -50 dBFS will not clear a room whose empty floor was measured at -36. Catching that
    here costs a second; catching it on the rig costs a window.
    """
    corpus = _corpus(
        tmp_path, [{"key": "quiet", "file": "quiet.wav", "text": "hello there"}]
    )
    _wav(corpus / "quiet.wav", seconds=2.0, amplitude=40)

    criteria = {c.ac: c for c in load._validate(_validate_args(corpus))}
    assert criteria["GATE"].verdict == "fail"
    assert "quiet" in criteria["GATE"].detail


def test_a_manifest_naming_a_missing_file_fails_loudly(tmp_path: Path) -> None:
    """⚠️ A setup error, raised — unlike everything in the play loop, which records and continues.

    A corpus is read once, before a window starts. A missing clip discovered on hour forty is a
    window with a hole in it; discovered here it is a typo.
    """
    corpus = _corpus(
        tmp_path, [{"key": "gone", "file": "gone.wav", "text": "not on disk"}]
    )
    with pytest.raises(ValueError, match="not on disk"):
        load._read_corpus(corpus)


def test_an_empty_manifest_is_refused(tmp_path: Path) -> None:
    """A corpus that declares nothing would run a window that plays nothing, silently."""
    with pytest.raises(ValueError, match="no utterances"):
        load._read_corpus(_corpus(tmp_path, []))


def test_the_room_caveat_is_always_reported(tmp_path: Path) -> None:
    """⚠️ The validator grades the FILE, and must never let that read as grading the room.

    Levels here are necessary and not sufficient: what decides is what arrives at the mic after the
    amp, the room and the distance. A validator that passed silently would be inviting exactly the
    "it looked fine on the laptop" failure.
    """
    corpus = _corpus(
        tmp_path, [{"key": "ok", "file": "ok.wav", "text": "what's on my calendar"}]
    )
    _wav(corpus / "ok.wav", seconds=2.0, amplitude=12000)

    criteria = {c.ac: c for c in load._validate(_validate_args(corpus))}
    assert criteria["ROOM"].verdict == "recorded"
    assert "not the room" in criteria["ROOM"].name


def test_a_corpus_nothing_could_judge_is_inconclusive_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠️ Off-Pi, Silero may be unavailable — and zero speech frames is what an unusable clip
    looks like, so "not measured" and "measured as unusable" must not share an answer (#380).
    """
    corpus = _corpus(
        tmp_path, [{"key": "ok", "file": "ok.wav", "text": "what's on my calendar"}]
    )
    _wav(corpus / "ok.wav", seconds=2.0, amplitude=12000)
    monkeypatch.setattr(load, "_speech_windows", lambda *a, **k: None)

    criteria = {c.ac: c for c in load._validate(_validate_args(corpus))}
    assert criteria["VAD"].verdict == "inconclusive"
    assert "ABSENT, not zero" in criteria["VAD"].detail


# ── the log ──────────────────────────────────────────────────────────────────────────────────


def test_the_log_survives_a_directory_that_cannot_be_written(tmp_path: Path) -> None:
    """⚠️ Losing the log must not stop the load.

    The log is how the report learns what the window contained, so it matters — but a generator
    that died because a disk filled would leave a window that looks idle, which is strictly worse
    than a window with an incomplete log.
    """
    blocker = tmp_path / "blocked"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    load._log_line(
        blocker / "load.jsonl", {"at": 1, "kind": "played"}
    )  # must not raise


def test_a_log_line_is_one_json_object_with_an_int_epoch(tmp_path: Path) -> None:
    """The shape `soak_pi._read_load_log` validates, and the only field it insists on."""
    path = tmp_path / "load.jsonl"
    load._log_line(path, {"at": 1787574327, "kind": "played", "note": "calendar"})
    load._log_line(path, {"at": 1787575227, "kind": "skipped_quiet", "note": "night"})

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        record = json.loads(line)
        assert isinstance(record["at"], int)


# ── the LOAD criterion in soak_pi.py ─────────────────────────────────────────────────────────


def _load_soak() -> Any:
    spec = importlib.util.spec_from_file_location(
        "soak_pi_load", _ROOT / "docs" / "demos" / "soak_pi.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["soak_pi_load"] = module
    spec.loader.exec_module(module)
    return module


soak = _load_soak()

_SINCE = 1_700_000_000
_UNTIL = _SINCE + 3 * 86_400


def _entries(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"at": _SINCE + i * 900, **r} for i, r in enumerate(records)]


def _samples(payloads: list[dict[str, Any] | None]) -> list[Any]:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE s (payload TEXT)")
    for payload in payloads:
        conn.execute(
            "INSERT INTO s VALUES (?)",
            (None if payload is None else json.dumps({"metrics": payload}),),
        )
    return list(conn.execute("SELECT * FROM s"))


def test_load_is_inconclusive_with_no_log_and_never_a_pass() -> None:
    """The whole point, stated as a verdict: absent is not idle.

    No log means no generator ran, which is a window measuring an unattended robot doing nothing.
    That is precisely the void-window case, so it must never read as a clean run.
    """
    criterion = soak._load_criterion(
        [], _samples([{"turns": 4}]), window=(_SINCE, _UNTIL)
    )
    assert criterion.verdict == "inconclusive"
    assert "ABSENT, not idle" in criterion.detail


def test_load_fails_when_an_utterance_produced_no_turn() -> None:
    """The graded claim: an utterance played owes a turn.

    A generator removes the empty-house confound that keeps LIVE's transition count merely
    reported — so this one can be graded without failing on a quiet evening.
    """
    entries = _entries(
        [{"kind": "played"}, {"kind": "played"}, {"kind": "no_turn", "note": "timeout"}]
    )
    criterion = soak._load_criterion(
        entries, _samples([{"turns": 2}]), window=(_SINCE, _UNTIL)
    )
    assert criterion.verdict == "fail"
    assert "1 produced no turn" in criterion.detail


def test_load_passes_when_every_utterance_was_answered() -> None:
    entries = _entries([{"kind": "played"}, {"kind": "played"}])
    criterion = soak._load_criterion(
        entries, _samples([{"turns": 2, "cost_usd": 0.09}]), window=(_SINCE, _UNTIL)
    )
    assert criterion.verdict == "pass"
    assert "every one of them answered" in criterion.detail


def test_load_names_the_cap_that_stopped_the_run() -> None:
    """A run that stops silently is indistinguishable from one that crashed."""
    entries = _entries(
        [{"kind": "played"}, {"kind": "capped", "note": "max-usd ($15.00)"}]
    )
    criterion = soak._load_criterion(
        entries, _samples([{"turns": 1}]), window=(_SINCE, _UNTIL)
    )
    assert any("max-usd" in row for row in criterion.rows)


def test_load_always_carries_the_proactive_suppression_caveat() -> None:
    """⚠️ Without this a reader diagnoses a proactivity regression that is not one.

    S10.4 rule 4 vetoes on ambient speech with no session opened, which is exactly what the
    generator manufactures, so `triggers_fired` collapsing during a load window is expected.
    """
    entries = _entries([{"kind": "played"}])
    criterion = soak._load_criterion(
        entries, _samples([{"turns": 1}]), window=(_SINCE, _UNTIL)
    )
    assert any("not a proactivity regression" in row for row in criterion.rows)


def test_a_malformed_log_line_is_counted_not_fatal(tmp_path: Path) -> None:
    """A generator killed mid-write must not take down a 72-hour report."""
    path = tmp_path / "load.jsonl"
    path.write_text(
        json.dumps({"at": _SINCE, "kind": "played"}) + "\n{ truncated\n",
        encoding="utf-8",
    )
    entries = soak._read_load_log(path)
    assert any(e.get("kind") == "_malformed" for e in entries)


def test_the_metered_total_survives_a_restart() -> None:
    """Same arithmetic as the generator's own meter, and for the same reason.

    Summing would double-count a window that restarted; taking the last would report a fresh
    process's small number over three days of work.
    """
    rows = _samples([{"turns": 40}, {"turns": 50}, {"turns": 3}, {"turns": 9}])
    assert soak._metric_total(rows, "turns") == pytest.approx(59)


def test_an_absent_metric_is_none_not_zero() -> None:
    assert soak._metric_total(_samples([{"turns": 4}]), "cost_usd") is None
    assert soak._metric_total(_samples([None, None]), "turns") is None
