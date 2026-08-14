"""The pure presence hysteresis filter (#222, SDS §9.1.3).

**The gate's headline criterion lives here** — *no flapping over a 1-hour desk recording* —
and because the filter is a pure function it is the one part of M8 that can be proven without
a camera. These are the adversarial cases the issue names, and they were written **before the
windows were tuned**: R-07's discipline from M7's retrieval eval (*build the eval set at the
milestone's start*), because tuned first, you unconsciously write the cases your current
thresholds already pass. The flicker and dropped-frame cases are the ones that matter.

Every test drives real sequences through :func:`replay` rather than poking state, so what is
asserted is what #225's committed trace will exercise on the same code path.
"""

from __future__ import annotations

import random

import pytest

from avid.domain.vision import (
    PresenceGained,
    PresenceLost,
    PresenceParams,
    PresenceState,
    replay,
    step,
)

# Deliberately round numbers, and deliberately NOT config/*.toml's shipped defaults: these
# tests describe the filter's behaviour, not the tuning. At 5 fps (0.2 s/frame) a 1 s gain
# window is 5 frames and a 4 s lose window is 20 — small enough to write sequences by hand.
_FPS = 5.0
_PERIOD = 1.0 / _FPS
_PARAMS = PresenceParams(
    confidence_threshold=0.6,
    gain_window_s=1.0,
    lose_window_s=4.0,
)


def _frames(
    pattern: list[tuple[bool, float]], *, start_s: float = 0.0
) -> list[tuple[float, bool, float]]:
    """Turn ``[(detected, confidence), ...]`` into a 5 fps timeline starting at *start_s*."""
    return [
        (start_s + index * _PERIOD, detected, confidence)
        for index, (detected, confidence) in enumerate(pattern)
    ]


def _seen(count: int, confidence: float = 0.9) -> list[tuple[bool, float]]:
    return [(True, confidence)] * count


def _empty(count: int) -> list[tuple[bool, float]]:
    return [(False, 0.0)] * count


# --- 1. a clean arrival ------------------------------------------------------


def test_a_clean_arrival_emits_one_gained_after_the_gain_window() -> None:
    """The base case, and it pins three separate things: exactly one decision, fired on the
    first frame at/after ``gain_window_s`` — not earlier, not later — and a ``confidence`` that
    is the **peak over the run**, not the tipping frame's own reading (#219 pinned that
    meaning; a filter that reported the last frame's score would satisfy a laxer test)."""
    timeline = _frames(
        _empty(5) + [(True, c) for c in (0.95, 0.7, 0.8, 0.65, 0.9, 0.75)]
    )
    _, decisions = replay(timeline, params=_PARAMS)

    assert len(decisions) == 1
    gained = decisions[0]
    assert isinstance(gained, PresenceGained)
    # First positive frame is at t=1.0; the window closes at t=2.0, the 6th positive frame.
    assert gained.at_s == pytest.approx(2.0)
    assert gained.confidence == pytest.approx(0.95)


def test_the_gain_window_is_not_fired_early() -> None:
    """One frame short of the window emits nothing. The complement of the test above — without
    it, a filter that fired on the *first* positive frame would still pass everything else."""
    _, decisions = replay(_frames(_seen(5)), params=_PARAMS)
    assert decisions == ()


# --- 2. a clean departure ----------------------------------------------------


def test_a_clean_departure_measures_absent_for_s_from_the_last_positive() -> None:
    """``absent_for_s`` is the assertion that keeps #224's nap honest. It is the gap from the
    **last positive detection** to the decision — explicitly *not* ``lose_window_s`` (which is
    one frame shorter, since the window is measured from the first negative frame) and
    explicitly not the time since the last frame, which would be one frame period."""
    timeline = _frames(_seen(10) + _empty(25))
    _, decisions = replay(timeline, params=_PARAMS)

    assert [type(d) for d in decisions] == [PresenceGained, PresenceLost]
    lost = decisions[1]
    assert isinstance(lost, PresenceLost)
    # Last positive frame t=1.8; first negative t=2.0; window closes at t=6.0.
    assert lost.at_s == pytest.approx(6.0)
    assert lost.absent_for_s == pytest.approx(4.2)
    assert lost.absent_for_s != pytest.approx(_PARAMS.lose_window_s)
    assert lost.absent_for_s != pytest.approx(_PERIOD)


# --- 3. a single dropped frame mid-presence ----------------------------------


def test_one_dropped_frame_mid_presence_emits_nothing() -> None:
    """A detector that misses one frame in twenty is a *good* detector. At 5 fps an hour is
    18,000 frames, so if a single miss could emit, the bus would carry hundreds of events an
    hour and §10.4's rule 3 would become noise. Also asserts the pending edge is cleared, not
    merely outrun — the state, not just the output."""
    pattern = _seen(10) + _empty(1) + _seen(10)
    state, decisions = replay(_frames(pattern), params=_PARAMS)

    assert [type(d) for d in decisions] == [PresenceGained]
    assert state.present is True
    assert state.edge_since_s is None


def test_a_five_percent_miss_rate_over_an_hour_still_emits_one_decision() -> None:
    """The gate criterion in miniature, and the reason the filter exists at all: 18,000 frames
    of someone sitting at a desk, with a seeded 5% independent per-frame miss rate, must yield
    **exactly one** decision. Unfiltered, this input contains roughly 900 falling edges."""
    rng = random.Random(8)
    pattern = [
        (rng.random() > 0.05, 0.9 if rng.random() > 0.05 else 0.0)
        for _ in range(18_000)
    ]
    _, decisions = replay(_frames(pattern), params=_PARAMS)

    assert [type(d) for d in decisions] == [PresenceGained]


# --- 4. a rapid flicker inside the exit window -------------------------------


def test_a_flicker_inside_the_exit_window_emits_one_decision_not_three() -> None:
    """off/on/off/on/off inside ``lose_window_s``, then sustained absence. The filter must emit
    exactly one ``lost``, and — the part a naive countdown gets wrong — timed from the **last**
    positive frame, not the first negative one. A decision per edge would be three."""
    pattern = (
        _seen(10)
        + _empty(3)
        + _seen(2)
        + _empty(3)
        + _seen(2)  # a re-arm; the run restarts here
        + _empty(30)
    )
    _, decisions = replay(_frames(pattern), params=_PARAMS)

    assert [type(d) for d in decisions] == [PresenceGained, PresenceLost]
    lost = decisions[1]
    assert isinstance(lost, PresenceLost)
    # The last positive frame is index 19 (t=3.8); absence must be measured from there.
    assert lost.absent_for_s > _PARAMS.lose_window_s
    assert lost.at_s == pytest.approx(3.8 + _PERIOD + _PARAMS.lose_window_s)


# --- 5. a slow fade at the confidence threshold ------------------------------


def test_a_slow_fade_across_the_confidence_threshold_does_not_oscillate() -> None:
    """The case a second (Schmitt) confidence threshold is usually added for — and the reason
    this filter does not need one. Confidence ramps 0.75 → 0.45 over 30 s with jitter
    straddling 0.6, so the *frame-level* verdict alternates dozens of times. The asymmetric
    time windows absorb it: at most one decision, and if there is one it is a ``lost``."""
    rng = random.Random(1)
    steps = 150
    pattern = _seen(20) + [
        (True, max(0.0, 0.75 - 0.30 * i / steps + rng.uniform(-0.05, 0.05)))
        for i in range(steps)
    ]
    _, decisions = replay(_frames(pattern), params=_PARAMS)

    after_arrival = decisions[1:]
    assert [type(d) for d in decisions[:1]] == [PresenceGained]
    assert len(after_arrival) <= 1
    assert all(isinstance(d, PresenceLost) for d in after_arrival)


# --- 6. an empty sequence ----------------------------------------------------


def test_an_empty_sequence_returns_the_initial_state_and_no_decisions() -> None:
    """Guards :func:`replay`'s base case. Trivial, and worth having: a filter that reported
    *nobody was ever here* would also never flap, which is why #225 pairs the flapping bound
    with ground truth."""
    initial = PresenceState(present=True, last_positive_s=12.0)
    state, decisions = replay([], params=_PARAMS, initial=initial)

    assert decisions == ()
    assert state == initial


# --- 7. a sequence that starts mid-presence ----------------------------------


def test_a_sequence_that_starts_mid_presence_emits_gained_once_and_never_again() -> (
    None
):
    """The trace opens with someone already in frame, so there is no rising edge to observe.
    From a fresh state the filter must still conclude presence, once. Driven from a state that
    already believes it, the identical trace must emit **nothing** — which is AC-6's edge
    idempotence stated as behaviour rather than as an internal invariant."""
    timeline = _frames(_seen(60))

    _, cold = replay(timeline, params=_PARAMS)
    assert [type(d) for d in cold] == [PresenceGained]

    _, warm = replay(
        timeline,
        params=_PARAMS,
        initial=PresenceState(present=True, last_positive_s=-1.0),
    )
    assert warm == ()


def test_it_never_emits_lost_while_already_absent() -> None:
    """The other half of AC-6. An hour of an empty room is the most common input this filter
    will ever see, and it must produce nothing at all."""
    _, decisions = replay(_frames(_empty(500)), params=_PARAMS)
    assert decisions == ()


def test_no_two_consecutive_decisions_share_a_kind() -> None:
    """Over a long pseudo-random sequence, decisions must strictly alternate. This is the
    general statement of edge idempotence — the individual cases above pin the corners; this
    one says nothing in between can produce gained/gained or lost/lost either."""
    rng = random.Random(20260815)
    pattern: list[tuple[bool, float]] = []
    present = False
    for _ in range(6_000):
        if rng.random() < 0.002:
            present = not present
        seen = rng.random() < (0.93 if present else 0.04)
        pattern.append((seen, rng.uniform(0.55, 1.0) if seen else 0.0))

    _, decisions = replay(_frames(pattern), params=_PARAMS)

    kinds = [type(d) for d in decisions]
    assert all(a is not b for a, b in zip(kinds, kinds[1:], strict=False))


# --- purity and determinism (AC-1) -------------------------------------------


def test_the_filter_is_deterministic_and_does_not_mutate_its_input() -> None:
    """AC-1 asks for determinism to be *enforced by a test*, not just claimed. Same input,
    same output, twice — and the caller's state object comes back unchanged, which is what
    makes replaying a trace from a checkpoint meaningful."""
    rng = random.Random(99)
    pattern = [(rng.random() > 0.3, rng.uniform(0.0, 1.0)) for _ in range(2_000)]
    timeline = _frames(pattern)
    initial = PresenceState()

    first_state, first = replay(timeline, params=_PARAMS, initial=initial)
    second_state, second = replay(timeline, params=_PARAMS, initial=initial)

    assert first == second
    assert first_state == second_state
    assert initial == PresenceState()


def test_step_returns_a_new_state_rather_than_mutating() -> None:
    before = PresenceState()
    after, decision = step(
        before, at_s=0.0, detected=True, confidence=0.9, params=_PARAMS
    )
    assert decision is None
    assert after is not before
    assert before.edge_since_s is None
    assert after.edge_since_s == pytest.approx(0.0)


def test_a_detection_below_the_threshold_is_not_a_positive_frame() -> None:
    """``confidence_threshold`` is injected (AC-7) and it is the filter's, not the detector's —
    the adapter's own floor sits deliberately lower so this is where the deciding happens."""
    below = _PARAMS.confidence_threshold - 0.01
    _, decisions = replay(_frames([(True, below)] * 60), params=_PARAMS)
    assert decisions == ()
