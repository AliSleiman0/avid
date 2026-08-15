"""The guards that would have caught #277 and #279: a vision config that cannot do its job.

Two defects, one shape. **#277** — `detector_scale = 2` shipped and detected a seated person in
**0 of ~75 frames**, not marginally but never. **#279** — `confidence_threshold = 0.6` with
`lose_window_s = 20` announced three departures in 17 minutes for someone who never left their
chair. Every automated M8 criterion passed throughout, because AC-1..AC-4 (CPU, fps, thermals,
event counts) are all satisfiable by a robot that sees nobody, and the resource run behind them
was recorded in an empty room. That is CLAUDE.md §7.1's "a gate that can pass on silence"
surviving in a new place.

Both settings were justified by **prose in a comment**, never an assertion, which is precisely
why they survived being wrong. These tests turn the measured relationships into executable ones,
so the next person to change `fps`, the scale or either window has to move a number that a test
is watching.

⚠️ **What these do NOT do.** None of them runs the detector, because that needs a face, and
this milestone's committed artefacts are bounding boxes and confidences — **never images**
(SDS §13, `assets/vision/README.md`). So these defend the *conditions* under which detection
was measured to work, not detection itself. A test that actually asserts a face is found
requires a committed fixture image and an explicit decision to allow one; until then the real
detector is exercised only on the Pi with a person in front of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from avid.core.config import Config, VisionConfig, load_config

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
_PI_TOML = _CONFIG_DIR / "pi.toml"

# Measured on the rig, 2026-08-15 (#277): a person at an ordinary desk distance, ov5647 at
# 640x480, presents a face this wide in FRAME pixels.
_DESK_FACE_PX = 93

# The smallest face, in MODEL-INPUT pixels, at which this YuNet export was observed to score a
# real person above `confidence_threshold`. At 93 px (scale 1) it scored 0.65 mean / 0.80 peak;
# at 46 px (scale 2) it peaked at 0.14 and never once cleared the adapter's own 0.3 floor.
# Units are the whole lesson: the retired claim ("down to 30 px") was in model-input pixels for
# a face 2.5x closer than anyone sits, so it was never a statement about this rig.
_MIN_DETECTABLE_FACE_PX = 90

# Below this many frames, one spurious detection and its neighbour can fake a presence gain.
_MIN_GAIN_FRAMES = 3.0

# The longest stretch a *present* person went without one frame at or above the threshold,
# measured over #225's first real desk hour (2026-08-15, 17.5 occupied minutes). This is the
# number `lose_window_s` has to clear: below it, the filter announces a departure for someone
# who never moved, which is exactly what 20 s did three times in 17 minutes (#279).
_WORST_GAP_WHILE_PRESENT_S = {0.30: 22.4, 0.35: 46.1, 0.40: 62.5, 0.60: 116.3}

# How far above that worst gap the window must sit. A margin over a measurement, not a fitted
# value — the costs are asymmetric (a late `presence_lost` delays a nap; an early one puts the
# robot to sleep on someone sitting in front of it), so this errs long.
_LOSE_WINDOW_MARGIN = 1.25

# Measured worst case for one capture+detect cycle at detector_scale=1, serialised on the one
# thread (§3.8.2). Median was 184 ms; the max is what has to fit, not the median.
_WORST_FRAME_MS = 247.0


def _vision(config: Config) -> VisionConfig:
    return config.vision


@pytest.mark.parametrize("source", [_PI_TOML], ids=["pi.toml"])
def test_a_desk_face_survives_the_downscale(source: Path) -> None:
    """#277 itself: the shipped scale must leave a real face big enough to be seen.

    This is the assertion whose absence let `detector_scale = 2` ship. It fails for 2 (46 px)
    and passes for 1 (93 px).
    """
    vision = _vision(load_config(source))
    face_at_model_input = _DESK_FACE_PX / vision.detector_scale
    assert face_at_model_input >= _MIN_DETECTABLE_FACE_PX, (
        f"detector_scale={vision.detector_scale} shrinks a {_DESK_FACE_PX} px desk face to "
        f"{face_at_model_input:.0f} px at the model input, below the {_MIN_DETECTABLE_FACE_PX} px "
        f"at which this export was measured to see a person (#277). At scale 2 this was not a "
        f"degradation — it was 0 detections in 75 frames."
    )


def test_the_default_config_is_also_usable() -> None:
    """The schema defaults matter as much as the file: a key missing from the machine's copy
    falls back to them silently (`deploy/PI_OPERATIONS.md` §3), so a bad default is a bad
    deployment nobody sees."""
    vision = VisionConfig()
    assert _DESK_FACE_PX / vision.detector_scale >= _MIN_DETECTABLE_FACE_PX


@pytest.mark.parametrize("source", [_PI_TOML], ids=["pi.toml"])
def test_one_frame_fits_in_the_frame_period(source: Path) -> None:
    """Capture and detect share one thread, so the WORST cycle must fit the period, not the
    median. At 5 fps with scale=1 the max was 124% of the period."""
    vision = _vision(load_config(source))
    period_ms = 1000.0 / vision.fps
    assert _WORST_FRAME_MS <= period_ms, (
        f"at {vision.fps} fps the period is {period_ms:.0f} ms, but a worst-case "
        f"capture+detect at detector_scale={vision.detector_scale} measured "
        f"{_WORST_FRAME_MS:.0f} ms (#277) — the loop would fall behind."
    )


@pytest.mark.parametrize("source", [_PI_TOML], ids=["pi.toml"])
def test_the_gain_window_is_still_several_frames(source: Path) -> None:
    """`gain_window_s` is stated in seconds but its anti-flap property is counted in frames, so
    it has to move when `fps` does. Dropping 5 -> 3 fps silently took 0.6 s from 3 frames to
    1.8, i.e. below the floor, without changing a single line of the filter."""
    vision = _vision(load_config(source))
    frames = vision.gain_window_s * vision.fps
    assert frames >= _MIN_GAIN_FRAMES, (
        f"gain_window_s={vision.gain_window_s} at {vision.fps} fps is {frames:.1f} frames, "
        f"under the {_MIN_GAIN_FRAMES:.0f} that stop a lone false positive and its neighbour "
        f"faking a presence gain (#277)."
    )


def _worst_gap_for(threshold: float) -> float:
    """The measured worst gap at the nearest tabulated threshold at or below *threshold*.

    Interpolating would invent precision the single hour does not support; taking the nearest
    lower tabulated value is the conservative read, because the gap grows with the threshold.
    """
    below = [t for t in _WORST_GAP_WHILE_PRESENT_S if t <= threshold + 1e-9]
    return (
        _WORST_GAP_WHILE_PRESENT_S[max(below)]
        if below
        else min(_WORST_GAP_WHILE_PRESENT_S.values())
    )


@pytest.mark.parametrize("source", [_PI_TOML], ids=["pi.toml"])
def test_the_lose_window_clears_the_measured_worst_gap(source: Path) -> None:
    """#279: a person working is not a person posing.

    They look down, turn to a second monitor, go to profile — and confidence follows the pose.
    `lose_window_s` has to outlast the longest such stretch, or the robot concludes that someone
    sitting right in front of it has left.
    """
    vision = _vision(load_config(source))
    worst = _worst_gap_for(vision.confidence_threshold)
    required = worst * _LOSE_WINDOW_MARGIN
    assert vision.lose_window_s >= required, (
        f"lose_window_s={vision.lose_window_s} at threshold "
        f"{vision.confidence_threshold} does not clear the measured {worst} s worst gap "
        f"(needs ≥ {required:.1f} s at a {_LOSE_WINDOW_MARGIN}x margin). At 20 s this produced "
        f"three departures in 17 minutes for a person who never moved (#279)."
    )


@pytest.mark.parametrize("source", [_PI_TOML], ids=["pi.toml"])
def test_the_threshold_stays_above_the_adapter_floor(source: Path) -> None:
    """The two thresholds do different jobs and must not collapse onto each other (SDS §3.9.1).

    The adapter's floor bounds how many boxes reach NMS — a cost. This one decides presence — a
    policy. Setting them equal makes "any detection at all" mean someone is here, and moves the
    decision out of the pure, replayable filter into the adapter.
    """
    from avid.adapters.face_detector import _SCORE_FLOOR

    vision = _vision(load_config(source))
    assert vision.confidence_threshold > _SCORE_FLOOR, (
        f"confidence_threshold={vision.confidence_threshold} is at or below the adapter's own "
        f"floor ({_SCORE_FLOOR}); presence would mean 'the adapter reported anything'."
    )


def test_the_guard_rejects_the_configuration_that_shipped() -> None:
    """Prove the guards bite (CLAUDE.md §7.1): the exact config from #277 must fail them.

    Without this, the tests above would pass just as happily against a constant.

    Note what is *not* asserted here. `gain_window_s = 0.6` at 5 fps is 3.0 frames — exactly the
    anti-flap floor, so the shipped config was **not** wrong about it. That window only became
    too short when this fix dropped the rate to 3 fps, which is the whole reason
    `test_the_gain_window_is_still_several_frames` exists: it guards a coupling that a future
    fps change would otherwise break silently, not a defect #277 found.
    """
    shipped = VisionConfig(detector_scale=2, fps=5, gain_window_s=0.6)

    # The regression: a desk face shrinks below what the model can see.
    assert _DESK_FACE_PX / shipped.detector_scale < _MIN_DETECTABLE_FACE_PX

    # And the fix's own hazard: scale=1 does not fit 5 fps, so raising the scale without
    # lowering the rate trades blindness for a loop that falls behind.
    assert _WORST_FRAME_MS > 1000.0 / shipped.fps

    # The shipped window was at the floor, not under it.
    assert shipped.gain_window_s * shipped.fps == pytest.approx(_MIN_GAIN_FRAMES)


def test_the_lose_window_guard_rejects_the_pair_that_flapped() -> None:
    """The #279 half: threshold 0.6 with a 20 s window must fail, in both of its terms."""
    flapped = VisionConfig(confidence_threshold=0.6, lose_window_s=20.0)
    worst = _worst_gap_for(flapped.confidence_threshold)

    assert (
        worst == _WORST_GAP_WHILE_PRESENT_S[0.60]
    )  # 116.3 s of silence while sitting there
    assert flapped.lose_window_s < worst * _LOSE_WINDOW_MARGIN

    # And the shipped pair clears it, so the guard is not simply always-fail.
    fixed = VisionConfig()
    assert (
        fixed.lose_window_s
        >= _worst_gap_for(fixed.confidence_threshold) * _LOSE_WINDOW_MARGIN
    )
