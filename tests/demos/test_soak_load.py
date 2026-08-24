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


def test_the_meter_charges_the_run_for_its_own_work_and_no_one_elses() -> None:
    """⚠️ The first reading is a BASELINE. It is not work this run did.

    The generator caps on "turns this run drove", and it meets a robot that has been up for hours
    talking to its owner. Counting that first reading charged the run for every one of those
    turns: on the rig it produced `max-turns (2) reached -- turns 8` before a single clip played,
    and over a 72-hour window one evening's conversation would silently stop the generator for the
    remaining three days -- the void window again, this time caused by the instrument.
    """
    meter = load._Meter()
    meter.observe(8, 0.17)  # the robot was already busy before we arrived
    assert meter.turns == 0, "the run was charged for turns it did not drive"
    assert meter.usd == pytest.approx(0.0)

    for turns, usd in ((11, 0.30), (15, 0.55)):
        meter.observe(turns, usd)
    assert meter.turns == 7, "rises after the baseline are this run's own work"
    assert meter.usd == pytest.approx(0.38)


def test_the_meter_survives_a_restart_without_forgetting_or_double_counting() -> None:
    """⚠️ The trap this exists for, and it is the same one #456 handles for transitions.

    `/metrics` counters are **process-scoped**, and `Restart=always` guarantees the robot restarts
    during a long window. Taking the last reading would forget everything before the restart and
    let the run spend its whole budget again; summing every reading would double-count and stop the
    run early. Only the rises are new work.
    """
    meter = load._Meter()
    meter.observe(40, 1.80)  # baseline — the robot was already at 40 turns
    meter.observe(50, 2.25)
    meter.observe(2, 0.09)  # restarted: the counter began again
    meter.observe(6, 0.27)

    assert meter.turns == 16, "a restart lost the earlier turns or replayed them"
    assert meter.usd == pytest.approx(0.72)


def test_an_absent_counter_moves_nothing() -> None:
    """A robot that did not report is not a robot that did nothing (#380)."""
    meter = load._Meter()
    meter.observe(5, 0.20)  # baseline
    meter.observe(9, 0.44)
    meter.observe(None, None)
    assert meter.turns == 4, "an absent reading moved the total"
    assert meter.usd == pytest.approx(0.24)


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


# ── the caps, driven through the real loop ───────────────────────────────────────────────────
#
# ⚠️ These exist because neutering the turn cap left the suite GREEN. The meter's arithmetic was
# covered and the loop that consumes it was not, so the only thing standing between an unattended
# generator and a weekend of billing had no test at all. A guard covered by nothing is a guard.


def _tick(monkeypatch: pytest.MonkeyPatch, step: float = 1.0) -> None:
    """Make the loop's monotonic deadline advance deterministically.

    ⚠️ Both no-cap cases below hang without this, and that is the point of having it: with
    ``--for-seconds 0`` the loop runs until a cap fires, and a robot that reports no turns can
    never trip one. Writing these tests found that -- a generator pointed at an unreachable robot
    spins forever, which is correct behaviour for an instrument that must outlive what it watches,
    but it means a test must bound it by the clock rather than by the caps.
    """
    now = iter(float(i) * step for i in range(10_000))
    monkeypatch.setattr(load.time, "monotonic", lambda: next(now))


def _drive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    metrics: list[dict[str, Any]],
    **over: Any,
) -> list[dict[str, Any]]:
    """Run the real `_run_load` against a scripted robot; return the log it wrote."""
    corpus = _corpus(
        tmp_path, [{"key": "ok", "file": "ok.wav", "text": "what's on my calendar"}]
    )
    _wav(corpus / "ok.wav", seconds=1.5, amplitude=12000)
    log = tmp_path / "load.jsonl"

    readings = iter(metrics)
    last = {"turns": 0, "cost_usd": 0.0}

    def fake_get_json(url: str, timeout: float) -> dict[str, Any] | None:
        if url.endswith("/state"):
            return {"state": "IDLE", "affect": "IDLE", "session": False}
        nonlocal last
        last = next(readings, last)
        return {"metrics": last, "absent": []}

    monkeypatch.setattr(load, "_get_json", fake_get_json)
    monkeypatch.setattr(load, "_play", lambda *a, **k: None)
    monkeypatch.setattr(load.time, "sleep", lambda _s: None)
    # ⚠️ Bounded by the clock as well as by the cap, so that NEUTERING a cap produces a failed
    # assertion instead of a hung suite. A hang is not a proof that the guard bites -- it is a
    # test that never got to say anything, and it is worse to debug than a red.
    _tick(monkeypatch)

    args = argparse.Namespace(
        config=_SIM_TOML,
        corpus=str(corpus),
        load_log=str(log),
        host="127.0.0.1",
        port=8787,
        timeout=1.0,
        interval=0.0,
        max_turns=100,
        max_usd=1000.0,
        for_seconds=20.0,
        settle_s=1.0,
        turn_timeout_s=1.0,
        poll_s=0.0,
        ignore_quiet_hours=True,
    )
    for key, value in over.items():
        setattr(args, key, value)

    assert load._run_load(args) == 0
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_the_turn_cap_stops_the_run_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """⚠️ THE ONLY CAP THAT EXISTS. S10.4's gate does not throttle reactive turns.

    Nothing in the robot would stop a generator spending a weekend's budget in an afternoon, so
    this loop is the whole of the protection — and it must also RECORD which cap fired, because a
    run that stops silently is indistinguishable from one that crashed.
    """
    records = _drive(
        monkeypatch,
        tmp_path,
        metrics=[{"turns": n, "cost_usd": 0.0} for n in range(0, 10)],
        max_turns=3,
    )
    capped = [r for r in records if r["kind"] == "capped"]
    assert capped, "the run never stopped -- the turn cap did not fire"
    assert "max-turns" in capped[0]["note"]
    assert len([r for r in records if r["kind"] == "played"]) <= 4


def test_the_spend_cap_stops_the_run_and_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Capped on measured `cost_usd`, never on `projected_monthly_usd`.

    The projection models 20 turns/day and ignores the observed rate, so at a generator's pace it
    stays healthy-looking while real spend runs many times the model. Only the true figure can
    bound a run.
    """
    records = _drive(
        monkeypatch,
        tmp_path,
        metrics=[{"turns": n, "cost_usd": 0.5 * n} for n in range(0, 10)],
        max_usd=1.2,
    )
    capped = [r for r in records if r["kind"] == "capped"]
    assert capped, "the run never stopped -- the spend cap did not fire"
    assert "max-usd" in capped[0]["note"]


def test_a_busy_robot_defers_the_clip_instead_of_playing_over_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """⚠️ A clip played over the robot's own reply is discarded against the echo floor.

    The money is spent and no turn happens, so the generator would be paying to make the window
    look busier than it was. `skipped_busy` is the honest record.
    """
    corpus = _corpus(
        tmp_path, [{"key": "ok", "file": "ok.wav", "text": "what's on my calendar"}]
    )
    _wav(corpus / "ok.wav", seconds=1.5, amplitude=12000)
    log = tmp_path / "load.jsonl"
    played: list[str] = []

    monkeypatch.setattr(
        load,
        "_get_json",
        lambda url, timeout: (
            {"state": "SPEAKING"}
            if url.endswith("/state")
            else {"metrics": {"turns": 0, "cost_usd": 0.0}}
        ),
    )
    monkeypatch.setattr(load, "_play", lambda *a, **k: played.append("played") or None)
    monkeypatch.setattr(load.time, "sleep", lambda _s: None)
    _tick(monkeypatch)

    args = argparse.Namespace(
        config=_SIM_TOML,
        corpus=str(corpus),
        load_log=str(log),
        host="127.0.0.1",
        port=8787,
        timeout=1.0,
        interval=0.0,
        max_turns=2,
        max_usd=10.0,
        for_seconds=8.0,
        # ⚠️ Must exceed the tick step, or `_wait_for` never polls even ONCE and reports
        # "not ready" whatever the state is -- passing for the wrong reason.
        settle_s=3.0,
        turn_timeout_s=3.0,
        poll_s=0.0,
        ignore_quiet_hours=True,
    )
    load._run_load(args)

    records = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert played == [], "a clip was played over the robot's own reply"
    assert any(r["kind"] == "skipped_busy" for r in records)


def test_an_unreachable_robot_is_recorded_not_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A generator that dies at 3 a.m. leaves a window that looks idle -- the failure it prevents."""
    corpus = _corpus(
        tmp_path, [{"key": "ok", "file": "ok.wav", "text": "what's on my calendar"}]
    )
    _wav(corpus / "ok.wav", seconds=1.5, amplitude=12000)
    log = tmp_path / "load.jsonl"

    monkeypatch.setattr(load, "_get_json", lambda url, timeout: None)
    monkeypatch.setattr(load.time, "sleep", lambda _s: None)
    _tick(monkeypatch)

    args = argparse.Namespace(
        config=_SIM_TOML,
        corpus=str(corpus),
        load_log=str(log),
        host="127.0.0.1",
        port=8787,
        timeout=1.0,
        interval=0.0,
        max_turns=2,
        max_usd=10.0,
        for_seconds=8.0,
        # ⚠️ Must exceed the tick step, or `_wait_for` never polls even ONCE and reports
        # "not ready" whatever the state is -- passing for the wrong reason.
        settle_s=3.0,
        turn_timeout_s=3.0,
        poll_s=0.0,
        ignore_quiet_hours=True,
    )
    assert load._run_load(args) == 0  # must not raise

    records = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(r["kind"] == "skipped_busy" for r in records)


def test_a_clip_at_the_wrong_rate_is_a_corpus_defect_not_an_absent_vad(
    tmp_path: Path,
) -> None:
    """⚠️ Found by running the validator against the shipped cue clips, on the rig.

    They are 24 kHz. Silero supports 16 kHz and 8 kHz only, so the model raised — and the code
    reported *"onnxruntime or the Silero model is absent here"* while both were installed and
    working. That is a report describing something other than the run, which is the defect family
    S7.1 exists to name, inside the tool written to prevent it.

    The rate cannot be fixed by resampling either: the repo's own resampler **refuses to
    downsample** without an anti-alias filter, deliberately. So a clip at the wrong rate is a
    corpus defect, it fails GATE, and it is never confused with a missing dependency.
    """
    corpus = _corpus(
        tmp_path, [{"key": "wrong", "file": "wrong.wav", "text": "recorded at 24k"}]
    )
    _wav(corpus / "wrong.wav", seconds=2.0, amplitude=12000, rate=24000)

    criteria = {c.ac: c for c in load._validate(_validate_args(corpus))}
    assert criteria["GATE"].verdict == "fail"
    assert "wrong" in criteria["GATE"].detail
    assert any("WRONG RATE" in row for row in criteria["CORPUS"].rows)
    # ...and it must NOT be reported as an absent VAD, which is a different fact entirely.
    assert "VAD" not in criteria, "a wrong-rate clip was blamed on a missing dependency"


def test_a_clip_that_is_not_mono_16_bit_cannot_be_played_and_says_so(
    tmp_path: Path,
) -> None:
    """The speaker writes S16_LE mono; anything else is a corpus defect, not a playback surprise."""
    corpus = _corpus(
        tmp_path, [{"key": "stereo", "file": "stereo.wav", "text": "two channels"}]
    )
    path = corpus / "stereo.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x10" * 32000)

    criteria = {c.ac: c for c in load._validate(_validate_args(corpus))}
    assert criteria["GATE"].verdict == "fail"
    assert any("NOT MONO 16-BIT" in row for row in criteria["CORPUS"].rows)


def test_a_sleeping_robot_is_spoken_to_not_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """⚠️ The state an unattended robot is actually IN, and the one this generator exists for.

    S3.10.3 has `(SLEEPING, audio.speech_started) -> LISTENING`: speech wakes the robot, which is
    the whole of M8's nap. The robot goes to SLEEPING ten minutes after the room empties -- so an
    empty room, which is exactly when an unattended window runs, means a SLEEPING robot for nearly
    all of it.

    Waiting only for IDLE would therefore have skipped **every utterance overnight** and produced a
    window that measured an idle robot: the void window again, this time caused by the tool built
    to prevent it. Found on the rig by finding it asleep at 18:01, before the first dry run rather
    than after a silent one.
    """
    corpus = _corpus(
        tmp_path, [{"key": "ok", "file": "ok.wav", "text": "what's on my calendar"}]
    )
    _wav(corpus / "ok.wav", seconds=1.5, amplitude=12000)
    log = tmp_path / "load.jsonl"
    played: list[str] = []

    # The robot stays SLEEPING throughout -- it is woken by speech and naps straight back, which
    # is what an empty room looks like. The TURN COUNTER is what says a turn happened; the state
    # reading cannot, and that is the point of the second assertion below.
    def _reading(url: str, timeout: float) -> dict[str, object]:
        if url.endswith("/state"):
            return {"state": "SLEEPING"}
        return {"metrics": {"turns": len(played), "cost_usd": 0.01 * len(played)}}

    monkeypatch.setattr(load, "_get_json", _reading)
    monkeypatch.setattr(load, "_play", lambda *a, **k: played.append("played") or None)
    monkeypatch.setattr(load.time, "sleep", lambda _s: None)
    _tick(monkeypatch)

    args = argparse.Namespace(
        config=_SIM_TOML,
        corpus=str(corpus),
        load_log=str(log),
        host="127.0.0.1",
        port=8787,
        timeout=1.0,
        interval=0.0,
        max_turns=2,
        max_usd=10.0,
        for_seconds=8.0,
        # ⚠️ Must exceed the tick step, or `_wait_for` never polls even ONCE and reports
        # "not ready" whatever the state is -- passing for the wrong reason.
        settle_s=3.0,
        turn_timeout_s=3.0,
        poll_s=0.0,
        ignore_quiet_hours=True,
    )
    load._run_load(args)

    records = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert played, (
        "a SLEEPING robot was treated as busy -- nothing would ever play overnight"
    )
    assert any(r["kind"] == "played" for r in records)
    assert not any(r["kind"] == "skipped_busy" for r in records)


def test_a_prior_conversation_does_not_cap_the_run_before_it_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """⚠️ Found on the rig, and it stopped a dry run at ZERO utterances.

    The generator meets a robot that has been up for hours. `/metrics turns` is process-scoped, so
    it already read 8 -- turns a human drove. Charging the run for them printed
    `STOP  max-turns (2) reached -- turns 8, $0.1730` before a clip played.

    Over a window that is the whole failure: the owner has a conversation on the first evening, the
    generator caps, and the remaining three days measure an idle process. The instrument produces
    the void window it was built to prevent, and does it quietly -- `capped` looks like a working
    cap, not a defect.
    """
    corpus = _corpus(
        tmp_path, [{"key": "ok", "file": "ok.wav", "text": "what's on my calendar"}]
    )
    _wav(corpus / "ok.wav", seconds=1.5, amplitude=12000)
    log = tmp_path / "load.jsonl"
    played: list[str] = []

    def _reading(url: str, timeout: float) -> dict[str, object]:
        if url.endswith("/state"):
            return {"state": "IDLE"}
        # 8 turns and $0.17 were already on the clock when we arrived.
        return {
            "metrics": {"turns": 8 + len(played), "cost_usd": 0.17 + 0.01 * len(played)}
        }

    monkeypatch.setattr(load, "_get_json", _reading)
    monkeypatch.setattr(load, "_play", lambda *a, **k: played.append("played") or None)
    monkeypatch.setattr(load.time, "sleep", lambda _s: None)
    _tick(monkeypatch)

    args = argparse.Namespace(
        config=_SIM_TOML,
        corpus=str(corpus),
        load_log=str(log),
        host="127.0.0.1",
        port=8787,
        timeout=1.0,
        interval=0.0,
        max_turns=2,
        max_usd=10.0,
        for_seconds=8.0,
        settle_s=3.0,
        turn_timeout_s=3.0,
        poll_s=0.0,
        ignore_quiet_hours=True,
    )
    load._run_load(args)

    records = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert played, (
        "the run capped on a prior conversation's turns and played nothing at all"
    )
    capped = [r for r in records if r["kind"] == "capped"]
    assert not capped or capped[0]["turns"] <= 2, (
        "the cap fired on turns this run did not drive"
    )


def test_a_turn_is_proved_by_the_counter_not_by_a_settled_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """⚠️ `turn_ok` used to be unfalsifiable overnight, and the rig recorded it as `true`.

    An unattended robot is SLEEPING before the clip and SLEEPING after it. Asking `/state` whether
    the robot is "settled again" therefore answers yes whether it ran a turn or heard nothing --
    and the first rig dry run duly logged `turn_ok: true, latency_s: 5.28` against a robot whose
    journal held NO ENTRIES for the surrounding eight minutes.

    That is a report describing something other than the run (CLAUDE.md §7.1), and it is the
    dangerous direction: a silent window full of `turn_ok: true` reads as a working load path.
    Here the robot never meters a turn, and the record must say so.
    """
    corpus = _corpus(
        tmp_path, [{"key": "ok", "file": "ok.wav", "text": "what's on my calendar"}]
    )
    _wav(corpus / "ok.wav", seconds=1.5, amplitude=12000)
    log = tmp_path / "load.jsonl"

    monkeypatch.setattr(
        load,
        "_get_json",
        lambda url, timeout: (
            {"state": "SLEEPING"}  # settled before AND after — deaf, not idle
            if url.endswith("/state")
            else {"metrics": {"turns": 0, "cost_usd": 0.0}}
        ),
    )
    monkeypatch.setattr(load, "_play", lambda *a, **k: None)
    monkeypatch.setattr(load.time, "sleep", lambda _s: None)
    _tick(monkeypatch)

    args = argparse.Namespace(
        config=_SIM_TOML,
        corpus=str(corpus),
        load_log=str(log),
        host="127.0.0.1",
        port=8787,
        timeout=1.0,
        interval=0.0,
        max_turns=5,
        max_usd=10.0,
        for_seconds=8.0,
        settle_s=3.0,
        turn_timeout_s=3.0,
        poll_s=0.0,
        ignore_quiet_hours=True,
    )
    load._run_load(args)

    records = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records, "the run recorded nothing at all"
    assert not any(r.get("turn_ok") for r in records), (
        "a deaf robot was recorded as having taken a turn -- turn_ok is unfalsifiable"
    )
    assert any(r["kind"] == "no_turn" for r in records), (
        "the clip that produced no turn was not reported as such"
    )
