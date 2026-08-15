"""Replay the committed detection traces through the filter — M8's flapping regression (#225).

The gate's headline criterion is *"no flapping over a 1-hour desk recording."* This is what
makes it permanent rather than a thing that was true once, on one afternoon, on one Pi: because
the filter is a pure function, CI replays a **real** recorded hour through it on every build, on
a laptop, with no camera and no model.

**Both halves are asserted, because either alone is trivially gameable.** A filter that reports
*nobody was ever here* never flaps; a filter that reports a decision per frame agrees with any
ground truth you like if you squint. So: the decisions must match the annotated arrivals and
departures within a stated tolerance, **and** the total decision count must stay inside a small
documented bound.

⚠️ If this test fails after a windows change, the legitimate response is to tune the windows
against the trace and record the before/after — **not** to loosen the bound. The bound is the
criterion (#225 AC-7).
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from avid.core.config import load_config
from avid.domain.vision import PresenceGained, PresenceLost, PresenceParams, replay

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TRACE_DIR = _REPO_ROOT / "assets" / "vision"
_GROUND_TRUTH = _TRACE_DIR / "ground_truth.json"

# How far a decision may sit from the ground-truth event it corresponds to. It is not a fudge
# factor: the filter is *supposed* to lag reality by its window, so a `gained` is expected
# `gain_window_s` after the arrival and a `lost` is expected `lose_window_s` after the
# departure. This tolerance is the slack around that expected lag — a human annotating "they
# left around 14:07" cannot be held to a frame.
_ANNOTATION_TOLERANCE_S = 6.0


@dataclass(frozen=True, slots=True)
class _Trace:
    name: str
    meta: dict[str, Any]
    frames: tuple[tuple[float, bool, float], ...]
    skipped: int


def _load(path: Path) -> _Trace:
    """Read a JSONL trace into the ``(t, detected, confidence)`` tuples the filter consumes.

    ``error`` rows are **skipped, not converted to absences**. A frame that could not be
    captured or judged is not evidence that the room was empty — the same rule
    ``PresenceService`` follows live, and feeding failures in as negatives is how a broken
    detector would nap the robot on someone sitting right there.
    """
    meta: dict[str, Any] = {}
    frames: list[tuple[float, bool, float]] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if "_meta" in row:
            meta = row["_meta"]
            continue
        if "error" in row:
            skipped += 1
            continue
        # ``c`` is absent on an empty frame (the recorder omits it to keep an hour under
        # 566 KB), so it defaults to 0.0 — which reads as "nothing seen", never as unknown.
        frames.append((float(row["t"]), int(row["n"]) > 0, float(row.get("c", 0.0))))
    return _Trace(name=path.name, meta=meta, frames=tuple(frames), skipped=skipped)


def _traces() -> list[Path]:
    return sorted(_TRACE_DIR.glob("*.jsonl"))


def _shipped_params() -> PresenceParams:
    """The **shipped** tuning, read from ``config/pi.toml`` rather than restated here.

    Read config, never restate it (CLAUDE.md §7.1): a literal in a test is drift with a delay
    fuse, and this test's entire purpose is to defend the values the robot actually runs."""
    vision = load_config(_REPO_ROOT / "config" / "pi.toml").vision
    return PresenceParams(
        confidence_threshold=vision.confidence_threshold,
        gain_window_s=vision.gain_window_s,
        lose_window_s=vision.lose_window_s,
    )


_needs_a_trace = pytest.mark.skipif(
    not _traces(),
    reason=(
        "no trace in assets/vision/ yet — #225's recording is an hour of ordinary desk "
        "activity on the Pi and cannot be synthesized (a curated or generated hour proves "
        "nothing). Record one with tools/record_vision_trace.py; this test then runs forever."
    ),
)


@_needs_a_trace
@pytest.mark.parametrize("path", _traces(), ids=lambda p: p.stem)
def test_the_trace_is_well_formed_and_carries_its_provenance(path: Path) -> None:
    """A trace whose detector version, camera or conditions are unknown cannot be re-derived,
    and #226 may need to. Also asserts the file is the *shape* the README documents, so a
    hand-edited or half-written trace fails here rather than as a confusing replay result."""
    trace = _load(path)
    assert trace.frames, f"{path.name} has no frames"
    for key in ("model", "camera", "fps", "detector_scale", "recorded_utc", "label"):
        assert key in trace.meta, f"{path.name} is missing provenance: {key}"
    # Monotonic and non-decreasing: the filter's whole arithmetic assumes it.
    times = [t for t, _, _ in trace.frames]
    assert times == sorted(times), f"{path.name} has out-of-order timestamps"
    assert all(0.0 <= c <= 1.0 for _, _, c in trace.frames)


@_needs_a_trace
@pytest.mark.parametrize("path", _traces(), ids=lambda p: p.stem)
def test_replaying_the_trace_does_not_flap(path: Path) -> None:
    """**The gate criterion, permanently defended.**

    The bound comes from the trace's own ground truth — decisions may not exceed the annotated
    transitions by more than a small documented slack — so it cannot be quietly widened without
    editing an annotation, which is a much louder change than editing a number.
    """
    trace = _load(path)
    truth = _load_ground_truth(trace.name)
    _, decisions = replay(trace.frames, params=_shipped_params())

    expected = len(truth["arrivals"]) + len(truth["departures"])
    bound = expected + truth.get("slack", 2)
    assert len(decisions) <= bound, (
        f"{trace.name}: {len(decisions)} decisions against {expected} real transitions "
        f"(bound {bound}). That is flapping. Tune the windows against this trace and record "
        f"the before/after — do NOT raise the bound; the bound is the criterion (#225 AC-7)."
    )


@_needs_a_trace
@pytest.mark.parametrize("path", _traces(), ids=lambda p: p.stem)
def test_the_decisions_match_what_actually_happened(path: Path) -> None:
    """The other half, and the one that stops "never flaps" being satisfied by "never notices".

    Every annotated arrival must have a ``gained`` after it (delayed by roughly the gain
    window), and every departure a ``lost``. A filter that emitted nothing at all would sail
    through the flapping bound above and fail here.
    """
    trace = _load(path)
    truth = _load_ground_truth(trace.name)
    params = _shipped_params()
    _, decisions = replay(trace.frames, params=params)

    gains = [d.at_s for d in decisions if isinstance(d, PresenceGained)]
    losses = [d.at_s for d in decisions if isinstance(d, PresenceLost)]

    for arrival in truth["arrivals"]:
        assert _matched(arrival, gains, params.gain_window_s), (
            f"{trace.name}: nobody was reported present after the arrival annotated at "
            f"{arrival}s — the robot would not have noticed a person sitting down"
        )
    for departure in truth["departures"]:
        assert _matched(departure, losses, params.lose_window_s), (
            f"{trace.name}: absence was never concluded after the departure annotated at "
            f"{departure}s — the robot would never nap in an empty room"
        )


def _matched(event_s: float, decisions: list[float], expected_lag_s: float) -> bool:
    """Whether some decision landed about ``expected_lag_s`` after ``event_s``.

    The lag is expected, not tolerated: the filter is *supposed* to wait out its window before
    concluding anything. What the tolerance covers is a human annotating from memory.
    """
    target = event_s + expected_lag_s
    return any(abs(at - target) <= _ANNOTATION_TOLERANCE_S for at in decisions)


def _load_ground_truth(trace_name: str) -> dict[str, Any]:
    assert _GROUND_TRUTH.is_file(), (
        "assets/vision/ground_truth.json is missing. Without it 'no flapping' is "
        "unfalsifiable: a filter that reports nobody was ever here also never flaps (AC-4)."
    )
    annotations = json.loads(_GROUND_TRUTH.read_text(encoding="utf-8"))
    assert trace_name in annotations, (
        f"{trace_name} has no ground-truth annotation. Every committed trace needs one — "
        f"an unannotated trace can only ever prove the half of the criterion that is "
        f"satisfied by seeing nothing."
    )
    return annotations[trace_name]


# --- the harness's own tests, which do NOT depend on a recorded trace --------
#
# The three tests above are skipped until #225's hour exists, and a skipped test proves
# nothing. These prove the *machinery* — the loader, the tolerance matcher, the bound — so a
# defect in this file is found now rather than after someone has spent an hour at a desk.
#
# ⚠️ The fixtures below are hand-built and **are not gate evidence**. A synthesized hour proves
# nothing about the robot (#225 AC-1 asks for *ordinary* activity, and a curated hour is worth
# less than none). They exist only so that when the real trace lands, the thing replaying it is
# known to work.


def _write_trace(path: Path, rows: list[dict[str, Any]], **meta: Any) -> Path:
    header = {
        "_meta": {
            "model": "face_detection_yunet_2026may.onnx",
            "camera": "640x480 RGB888",
            "fps": 5,
            "detector_scale": 2,
            "recorded_utc": "2026-08-15T00:00:00Z",
            "label": "harness self-test, not gate evidence",
            **meta,
        }
    }
    path.write_text(
        "\n".join(json.dumps(row) for row in [header, *rows]) + "\n", encoding="utf-8"
    )
    return path


def test_the_loader_skips_error_rows_rather_than_reading_them_as_absence(
    tmp_path: Path,
) -> None:
    """The rule ``PresenceService`` follows live, and the loader must follow it too: a frame
    that could not be judged is **not** evidence the room was empty. If the replay converted
    errors to negatives, a trace recorded through a flaky camera would show a departure that
    never happened — and the tuning done against it would be tuning against an artefact."""
    path = _write_trace(
        tmp_path / "t.jsonl",
        [
            {"t": 0.0, "n": 1, "c": 0.9, "box": [0, 0, 10, 10]},
            {"t": 0.2, "error": "TimeoutError"},
            {"t": 0.4, "n": 1, "c": 0.9, "box": [0, 0, 10, 10]},
        ],
    )
    trace = _load(path)
    assert trace.skipped == 1
    assert [detected for _, detected, _ in trace.frames] == [True, True]


def test_the_loader_defaults_the_omitted_fields_of_an_empty_frame(
    tmp_path: Path,
) -> None:
    """The recorder omits ``c``/``box`` when nothing was seen, to keep an hour in the hundreds
    of KB. Their absence must read as *nothing seen* — zero confidence — never as unknown."""
    path = _write_trace(tmp_path / "t.jsonl", [{"t": 0.0, "n": 0}])
    assert _load(path).frames == ((0.0, False, 0.0),)


def test_the_matcher_accepts_the_expected_lag_and_rejects_absence_and_lateness() -> (
    None
):
    """The matcher centres on ``event + window`` because the filter is *supposed* to lag: it
    waits out its window before concluding anything. What the tolerance covers is the human,
    not the filter — someone annotating "they left around 14:07" from memory cannot be held to
    a frame, and #225 AC-4 asks for exactly that kind of annotation.

    ⚠️ **A consequence worth stating, because I asserted the opposite first and was wrong.**
    The tolerance (6 s) is far larger than ``gain_window_s`` (0.6 s), so *for arrivals* this
    matcher cannot distinguish the correct lag from no lag at all — a filter with the
    hysteresis ripped out would still be "matched" here. That is not a hole; it is the division
    of labour AC-5 insists on. This half asks **did the robot notice the person**, and the
    flapping bound asks **did it debounce**. *"Either alone is trivially gameable"* is the
    issue's own wording, and this is the shape that makes it true. Tightening the tolerance to
    close it would instead start failing on honest annotation error, which is far more common
    than a filter shipped with no hysteresis.
    """
    arrival, window = 100.0, 0.6
    assert _matched(arrival, [arrival + window], window)
    assert _matched(arrival, [arrival + window + _ANNOTATION_TOLERANCE_S / 2], window)
    # Absent and far-too-late are the two failures this half genuinely owns.
    assert not _matched(arrival, [], window)
    assert not _matched(
        arrival, [arrival + window + _ANNOTATION_TOLERANCE_S * 2], window
    )
    # ...and for the long window the lag IS resolvable, which is the departure case (20 s).
    departure, lose = 100.0, 20.0
    assert _matched(departure, [departure + lose], lose)
    assert not _matched(departure, [departure], lose), (
        "a loss concluded at the instant of departure means the exit window was not waited "
        "out at all — for the long window the matcher does catch that"
    )


def test_a_flapping_trace_fails_the_bound_and_a_calm_one_passes(tmp_path: Path) -> None:
    """The bound has to *bite*. A detector that drops one frame in three, replayed through a
    filter with a too-short exit window, produces the hundreds-of-events-an-hour §9.1.3 warns
    about; the same activity through the shipped windows produces two decisions.

    This is the assertion that would have caught a filter change that broke debouncing — the
    real trace makes it evidence about the *room*, but the machinery is proven here.
    """
    rng = random.Random(4)
    # Ten minutes of someone present, missed every third frame or so.
    rows = [
        {
            "t": round(i * 0.2, 3),
            **({"n": 1, "c": 0.9} if rng.random() > 0.33 else {"n": 0}),
        }
        for i in range(3_000)
    ]
    path = _write_trace(tmp_path / "flappy.jsonl", rows)
    frames = _load(path).frames

    shipped = _shipped_params()
    _, calm = replay(frames, params=shipped)
    assert len(calm) == 1, "the shipped windows must absorb a 33% miss rate"

    no_hysteresis = PresenceParams(
        confidence_threshold=shipped.confidence_threshold,
        gain_window_s=0.0,
        lose_window_s=0.0,
    )
    _, flapping = replay(frames, params=no_hysteresis)
    assert len(flapping) > 100, (
        "without hysteresis this input must flap — if it does not, the fixture is too easy "
        "and the bound above is not actually being exercised"
    )
