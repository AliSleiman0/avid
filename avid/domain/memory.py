"""The ``Fact`` value, the ``memory.*`` events, and §7.7's pure scoring (#116).

The pure layer M7 "It remembers" is built on. Everything else in the milestone is I/O —
SQLite (#117), ONNX embeddings (#119), a websocket (#124); this module is the part that
is *only logic*, and §14.2 names it tier-1 territory: *"§7.7's scoring — recency decay at
known Δt, min-max normalisation, top-k ordering."* Ranking bugs are **silent** (R-07): a
wrong decay constant does not crash, it just makes the robot feel senile six weeks later.
Pure functions with exhaustive unit tests catch that in 4 ms.

Three things live here, all pure (P1): no I/O, no clock, no ``numpy``, no config import.

- :class:`Fact` — the §8.3 columns the application needs, **minus the embedding BLOB**:
  vectors belong to the index (§8.5, ADR-005), not to the domain value. Timestamps are
  epoch **seconds** (§8.2) — deliberately *not* the ``Event`` envelope's ``timestamp_ms``;
  do not cross-import the two conventions.
- The four :class:`~avid.domain.events.Event` subclasses already **normative in §9.1.3** —
  this module gives them types, it does not invent them. Their names already sit in the
  test catalog (``tests/domain/test_events.py::CATALOG``).
- The §7.7 scoring: :func:`recency_decay` and :func:`rank_candidates`, the Generative
  Agents model (Park et al., UIST 2023) — ``score = α·recency + β·importance + γ·relevance``,
  each component min-max normalised, equal weights as the published baseline. ``Δt`` and
  ``k`` are **injected** (from config, §7.7 / P7), never read or hard-coded here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Literal
from uuid import UUID

from avid.domain.events import Event

# The six fact kinds, matching the §8.3 ``CHECK (kind IN (...))`` constraint **exactly** so
# the domain type and the database constraint cannot drift (AC-1). ``FACT_KINDS`` backs the
# ``Literal`` as a runtime tuple a drift test can assert against.
FactKind = Literal[
    "identity", "preference", "routine", "relationship", "event", "other"
]
FACT_KINDS: tuple[FactKind, ...] = (
    "identity",
    "preference",
    "routine",
    "relationship",
    "event",
    "other",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class Fact:
    """A single durable thing the robot was told about the user (§8.3).

    Carries the columns the application reasons over — text, kind, importance, confidence,
    timestamps, supersession pointers, provenance — but **not** the ``embedding`` BLOB:
    the vector is the index's concern (§8.5), and keeping it out of the value is what lets
    the domain stay ``numpy``-free (AC-6).

    Timestamps are epoch **seconds**, UTC (§8.2) — note this differs from the ``Event``
    envelope's ``timestamp_ms`` (milliseconds); the two conventions are deliberately not
    interchangeable. Range invariants (``importance`` 1–10, ``confidence`` 0–1) are enforced
    by the §8.3 ``CHECK`` at the storage boundary, not re-validated here — the value stays a
    plain frozen record, like :class:`~avid.domain.conversation.TokenUsage`.
    """

    id: int  # the §8.3 PRIMARY KEY; assigned by the repository (#117) on insert
    text: str  # the fact, in the user's own words (§7.6)
    kind: FactKind  # one of FACT_KINDS — matches the §8.3 CHECK exactly
    importance: int  # LLM-rated 1–10 at write time (§7.6); stored, never recomputed
    confidence: float = 1.0  # 0–1; how sure we are the fact is true (§8.3)

    created_at: int  # epoch SECONDS, UTC (§8.2) — when first stored
    last_accessed_at: int  # epoch SECONDS; drives recency decay (§7.7)
    access_count: int = 0  # times retrieved; §7.9 reflection signal

    superseded_by: int | None = None  # id of the fact that replaced this one (§7.8)
    superseded_at: int | None = (
        None  # epoch SECONDS when superseded; paired with the above
    )

    derived_from: tuple[int, ...] = ()  # source fact ids, for reflections (§7.9)
    source_correlation_id: UUID | None = None  # the turn this fact came from (§3.12.2)


# ── memory.* events (§9.1.3, SDS:2049–2052) — facts MemoryService will publish after a
# durable, direct-call write (§9.1.4). Names + payloads are normative and CI-enforced by the
# event-catalog-drift check; each carries a validated <domain>.<past_tense_verb> name (P4). ──


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryFactStored(Event):
    """A fact was committed to the store (SDS §9.1.3, SDS:2049).

    Published by ``MemoryService`` **after** the write is durable (§3.7.3) — the notification
    follows the fact, never precedes it. ``BehaviorService`` subscribing to this is how UC-02
    becomes UC-03 with zero coupling: ``MemoryService`` has no idea a behaviour engine exists.
    Queue policy DROP_NEWEST.
    """

    name: ClassVar[str] = "memory.fact_stored"

    fact_id: int  # the stored fact's §8.3 id
    kind: FactKind  # its kind, so subscribers can filter without a store read
    importance: int  # 1–10, the LLM's write-time rating


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryFactSuperseded(Event):
    """One fact replaced another on write (SDS §9.1.3, SDS:2050).

    Soft supersession (§7.8): the old fact is not deleted, it is pointed at the new one so
    temporal reasoning survives ("what did I *used* to drink?"). Published after the
    ``UPDATE ... SET superseded_by`` commits. Queue policy DROP_NEWEST.
    """

    name: ClassVar[str] = "memory.fact_superseded"

    old_id: int  # the fact now marked superseded
    new_id: int  # the fact that replaced it


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryFactDeleted(Event):
    """A fact was hard-deleted (SDS §9.1.3, SDS:2051).

    The "forget that" path (UC-05): a cascading DELETE from SQLite *and* the vector index,
    published after both are gone. Distinct from supersession — this leaves no trace to
    reason over. Queue policy DROP_NEWEST.
    """

    name: ClassVar[str] = "memory.fact_deleted"

    fact_id: int  # the id that no longer exists


@dataclass(frozen=True, slots=True, kw_only=True)
class MemoryRecallCompleted(Event):
    """A recall finished (SDS §9.1.3, SDS:2052).

    Observability's feed for R-07 — the number the retrieval eval set (#115) tracks in the
    field. ``n_returned`` is how many facts cleared the relevance floor (may be 0); a real
    hybrid retriever answering a negative returns nothing. ``latency_ms`` is monotonic-derived
    (§9.1.1); the <150 ms budget (§7.7) is judged on it. Queue policy DROP_NEWEST.
    """

    name: ClassVar[str] = "memory.recall_completed"

    query: str  # the recall query text
    n_returned: int  # how many facts were returned (0 for a clean miss)
    latency_ms: float  # wall length of the recall, monotonic-derived


# ── §7.7 scoring — the Generative Agents model, pure. Δt and k are injected (P7); no clock,
# no numpy, no config import (the matmul lives in the index adapter, AC-6). ──


@dataclass(frozen=True, slots=True, kw_only=True)
class ScoreWeights:
    """The three §7.7 weights (α, β, γ). Defaults are Park et al.'s **equal-weight baseline**
    (α=β=γ=1) — *"deviating from a published baseline before measuring is how you end up
    tuning noise."* The service overrides these from ``WeightsConfig`` (P7); the defaults keep
    the domain function callable and testable without config."""

    recency: float = 1.0
    importance: float = 1.0
    relevance: float = 1.0


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrievalCandidate:
    """One fact the hybrid retriever (#120) surfaced, reduced to the **plain scalars** scoring
    needs (AC-6). Decoupled from :class:`Fact` on purpose: scoring stays free of the full
    record, and the caller supplies ``age_days`` (computed from a clock it owns, never here)
    and ``relevance`` (cosine, from the index)."""

    fact_id: int  # which fact this candidate is
    importance: int  # the fact's stored 1–10 importance (§7.6)
    age_days: float  # Δt since last_accessed, in days — passed in, never read (AC-4)
    relevance: float  # cosine similarity, query embedding vs fact embedding (§7.7)


@dataclass(frozen=True, slots=True, kw_only=True)
class ScoredCandidate:
    """A candidate with its combined §7.7 score. :func:`rank_candidates` returns these
    best-first, truncated to k."""

    fact_id: int
    score: float


def recency_decay(age_days: float, half_life_days: float) -> float:
    """Exponential recency: ``0.5 ** (age_days / half_life_days)`` (§7.7, AC-4).

    ``1.0`` at age 0, halving every ``half_life_days`` (14 days in config — *not* Park's
    0.995/sandbox-hour, which is simulation time). ``age_days`` is **passed in**, computed by
    the caller from a clock it owns; this function reads no clock and imports no ``time``.
    """
    # float() pins the result type: typeshed's float.__pow__ overload widens to Any.
    return float(0.5 ** (age_days / half_life_days))


def _min_max(values: Sequence[float]) -> list[float]:
    """Min-max normalise ``values`` to [0,1] (§7.7: each component normalised before combining).

    Degenerate rule (AC-6): when ``max == min`` — a single candidate, or all values equal —
    the component carries no information, so every candidate gets a constant ``1.0``. Because
    the constant is identical across candidates it cannot change their relative order; the
    choice is documented purely so the behaviour is defined rather than a division by zero.
    """
    lo, hi = min(values), max(values)
    span = hi - lo
    if span == 0.0:
        return [1.0] * len(values)
    return [(v - lo) / span for v in values]


def rank_candidates(
    candidates: Sequence[RetrievalCandidate],
    *,
    weights: ScoreWeights,
    half_life_days: float,
    k: int,
) -> tuple[ScoredCandidate, ...]:
    """Score and rank retrieval candidates by the §7.7 model, returning the top ``k`` best-first.

    ``score = α·recency + β·importance + γ·relevance``, each of the three components min-max
    normalised across *this candidate set* to [0,1] (:func:`_min_max`) before the weighted sum.
    Pure — ``weights``, ``half_life_days`` and ``k`` are all injected (from config; §7.7, P7),
    so the domain hard-codes none of them (AC-5).

    Determinism (AC-5): candidates are ordered by ``(-score, fact_id)`` — highest score first,
    ascending ``fact_id`` breaking ties, so the result never depends on input or dict ordering.
    An empty candidate list returns ``()``.
    """
    if not candidates:
        return ()
    recency = _min_max([recency_decay(c.age_days, half_life_days) for c in candidates])
    importance = _min_max([float(c.importance) for c in candidates])
    relevance = _min_max([c.relevance for c in candidates])
    scored = [
        ScoredCandidate(
            fact_id=c.fact_id,
            score=(
                weights.recency * recency[i]
                + weights.importance * importance[i]
                + weights.relevance * relevance[i]
            ),
        )
        for i, c in enumerate(candidates)
    ]
    scored.sort(key=lambda s: (-s.score, s.fact_id))
    return tuple(scored[:k])
