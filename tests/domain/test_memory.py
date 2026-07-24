"""Tier-1 tests for the Fact value, the memory.* events, and §7.7 scoring (#116).

Pure, no I/O, no clock — milliseconds (§14.2). The scoring tests are the valuable ones:
ranking bugs are silent (R-07), so the degenerate cases (one candidate, all-equal,
empty, importance extremes) and the tie-break determinism are what earn their keep.
"""

from __future__ import annotations

import dataclasses
import random
from uuid import uuid4

import pytest

from avid.domain import (
    FACT_KINDS,
    Fact,
    MemoryFactDeleted,
    MemoryFactStored,
    MemoryFactSuperseded,
    MemoryRecallCompleted,
    RetrievalCandidate,
    ScoredCandidate,
    ScoreWeights,
    rank_candidates,
    recency_decay,
    validate_event_name,
)

# The §8.3 CHECK (kind IN (...)) set, hard-coded here as the drift guard: the domain
# Literal and the database constraint must never diverge (AC-1).
SDS_8_3_KINDS = {"identity", "preference", "routine", "relationship", "event", "other"}


# ── Fact (AC-1) ─────────────────────────────────────────────────────────────


def test_fact_kinds_match_the_sds_8_3_check_exactly() -> None:
    assert set(FACT_KINDS) == SDS_8_3_KINDS


def test_fact_constructs_and_applies_defaults() -> None:
    f = Fact(
        id=1,
        text="my sister's name is maya",
        kind="relationship",
        importance=8,
        created_at=1_700_000_000,
        last_accessed_at=1_700_000_000,
    )
    assert f.confidence == 1.0
    assert f.access_count == 0
    assert f.superseded_by is None
    assert f.superseded_at is None
    assert f.derived_from == ()
    assert f.source_correlation_id is None


def test_fact_carries_supersession_and_provenance() -> None:
    cid = uuid4()
    f = Fact(
        id=9,
        text="i drink tea now",
        kind="preference",
        importance=4,
        confidence=0.9,
        created_at=1,
        last_accessed_at=2,
        access_count=3,
        superseded_by=None,
        superseded_at=None,
        derived_from=(1, 2),
        source_correlation_id=cid,
    )
    assert f.derived_from == (1, 2)
    assert f.source_correlation_id == cid


def test_fact_is_frozen() -> None:
    f = Fact(
        id=1, text="x", kind="other", importance=1, created_at=0, last_accessed_at=0
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.importance = 5  # type: ignore[misc]


def test_fact_has_no_embedding_field() -> None:
    # The vector belongs to the index (§8.5), never the domain value (AC-6 / Notes).
    assert "embedding" not in {f.name for f in dataclasses.fields(Fact)}


# ── memory.* events (AC-2) ──────────────────────────────────────────────────

_ENVELOPE = {
    "event_id": uuid4(),
    "correlation_id": uuid4(),
    "timestamp_ms": 1,
    "monotonic_ns": 2,
    "source": "MemoryService",
}


def test_fact_stored_carries_its_catalogued_name_and_payload() -> None:
    e = MemoryFactStored(**_ENVELOPE, fact_id=7, kind="identity", importance=10)
    assert e.name == "memory.fact_stored"
    assert (e.fact_id, e.kind, e.importance) == (7, "identity", 10)


def test_fact_superseded_carries_its_catalogued_name_and_payload() -> None:
    e = MemoryFactSuperseded(**_ENVELOPE, old_id=3, new_id=9)
    assert e.name == "memory.fact_superseded"
    assert (e.old_id, e.new_id) == (3, 9)


def test_fact_deleted_carries_its_catalogued_name_and_payload() -> None:
    e = MemoryFactDeleted(**_ENVELOPE, fact_id=42)
    assert e.name == "memory.fact_deleted"
    assert e.fact_id == 42


def test_recall_completed_carries_its_catalogued_name_and_payload() -> None:
    e = MemoryRecallCompleted(
        **_ENVELOPE, query="where do i work?", n_returned=3, latency_ms=12.5
    )
    assert e.name == "memory.recall_completed"
    assert (e.query, e.n_returned, e.latency_ms) == ("where do i work?", 3, 12.5)


@pytest.mark.parametrize(
    "name",
    [
        "memory.fact_stored",
        "memory.fact_superseded",
        "memory.fact_deleted",
        "memory.recall_completed",
    ],
)
def test_event_names_pass_the_p4_validator(name: str) -> None:
    validate_event_name(name)  # raises on a malformed / non-past-tense name


def test_memory_events_are_frozen() -> None:
    e = MemoryFactDeleted(**_ENVELOPE, fact_id=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.fact_id = 2  # type: ignore[misc]


# ── recency_decay (§7.7, AC-4) ──────────────────────────────────────────────


def test_recency_is_one_at_age_zero() -> None:
    assert recency_decay(0.0, 14.0) == 1.0


def test_recency_halves_every_half_life() -> None:
    assert recency_decay(14.0, 14.0) == pytest.approx(0.5)
    assert recency_decay(28.0, 14.0) == pytest.approx(0.25)


def test_recency_is_strictly_decreasing_in_age() -> None:
    ages = [0.0, 1.0, 7.0, 14.0, 30.0, 100.0]
    decays = [recency_decay(a, 14.0) for a in ages]
    assert all(earlier > later for earlier, later in zip(decays, decays[1:]))


# ── rank_candidates (§7.7, AC-3/AC-5/AC-6) ──────────────────────────────────

_EQUAL = ScoreWeights()  # α=β=γ=1


def _cand(
    fact_id: int, importance: int, age_days: float, relevance: float
) -> RetrievalCandidate:
    return RetrievalCandidate(
        fact_id=fact_id, importance=importance, age_days=age_days, relevance=relevance
    )


def test_empty_candidates_return_empty() -> None:
    assert rank_candidates([], weights=_EQUAL, half_life_days=14.0, k=5) == ()


def test_single_candidate_survives_min_max() -> None:
    # max == min on every component → the [1.0] degenerate branch, not a ZeroDivisionError.
    (only,) = rank_candidates(
        [_cand(1, 5, 3.0, 0.7)], weights=_EQUAL, half_life_days=14.0, k=5
    )
    assert only.fact_id == 1
    assert only.score == pytest.approx(3.0)  # 1·1 + 1·1 + 1·1, all components uniform


def test_ranks_best_combined_score_first() -> None:
    # c2 dominates on all three axes → must rank first.
    ranked = rank_candidates(
        [_cand(1, 1, 30.0, 0.1), _cand(2, 10, 0.0, 0.9)],
        weights=_EQUAL,
        half_life_days=14.0,
        k=5,
    )
    assert [s.fact_id for s in ranked] == [2, 1]


def test_top_k_truncates() -> None:
    cands = [_cand(i, i, float(i), i / 10) for i in range(1, 6)]
    ranked = rank_candidates(cands, weights=_EQUAL, half_life_days=14.0, k=2)
    assert len(ranked) == 2


def test_tie_break_is_deterministic_under_shuffle() -> None:
    # All identical scalars → all scores equal → order must fall back to ascending fact_id,
    # regardless of input order (AC-5: never dict / insertion order).
    base = [_cand(fid, 5, 3.0, 0.5) for fid in (4, 1, 3, 2, 5)]
    shuffled = base[:]
    random.Random(0).shuffle(shuffled)
    ranked = rank_candidates(shuffled, weights=_EQUAL, half_life_days=14.0, k=5)
    assert [s.fact_id for s in ranked] == [1, 2, 3, 4, 5]


def test_zeroing_a_weight_changes_the_order() -> None:
    # c1 wins on relevance only; c2 wins on recency+importance. With relevance zeroed, c2 leads.
    cands = [_cand(1, 1, 30.0, 1.0), _cand(2, 10, 0.0, 0.0)]
    relevance_only = ScoreWeights(recency=0.0, importance=0.0, relevance=1.0)
    no_relevance = ScoreWeights(recency=1.0, importance=1.0, relevance=0.0)
    assert (
        rank_candidates(cands, weights=relevance_only, half_life_days=14.0, k=5)[
            0
        ].fact_id
        == 1
    )
    assert (
        rank_candidates(cands, weights=no_relevance, half_life_days=14.0, k=5)[
            0
        ].fact_id
        == 2
    )


def test_all_equal_scores_is_stable_and_ordered() -> None:
    ranked = rank_candidates(
        [_cand(3, 7, 1.0, 0.4), _cand(1, 7, 1.0, 0.4), _cand(2, 7, 1.0, 0.4)],
        weights=_EQUAL,
        half_life_days=14.0,
        k=5,
    )
    assert [s.fact_id for s in ranked] == [1, 2, 3]
    assert all(s.score == pytest.approx(3.0) for s in ranked)


def test_importance_extremes_normalise_to_the_endpoints() -> None:
    # importance 1 and 10, everything else equal → normalised importance 0.0 vs 1.0, so the
    # importance-10 fact leads by exactly one weighted unit.
    ranked = rank_candidates(
        [_cand(1, 1, 5.0, 0.5), _cand(2, 10, 5.0, 0.5)],
        weights=_EQUAL,
        half_life_days=14.0,
        k=5,
    )
    top, bottom = ranked
    assert top.fact_id == 2
    assert top.score - bottom.score == pytest.approx(1.0)


def test_returns_scored_candidates() -> None:
    ranked = rank_candidates(
        [_cand(1, 5, 3.0, 0.7)], weights=_EQUAL, half_life_days=14.0, k=5
    )
    assert isinstance(ranked[0], ScoredCandidate)
