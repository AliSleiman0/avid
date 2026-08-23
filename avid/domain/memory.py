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
class RoutineSpec:
    """The machine-readable half of a routine fact — one ``routines`` row, as written (§8.3, §10.3).

    A ``routine``-kind fact says *"the user drinks coffee every day at 08:00"* in the user's own
    words. That sentence is what §7.7 retrieves and what the model reads; it is not something §10.3
    can put in a min-heap. This is the other half: the same routine, in RFC 5545, so a scheduler can
    resolve it.

    It arrives **from the model**, on ``remember_fact``'s ``schedule`` argument (§6.6). The model has
    already parsed "every day at 8 AM" out of speech in order to write the fact text, so this asks
    for the structured form of something it demonstrably had in hand — rather than reading the text
    back and re-deriving it locally, where a heuristic that reads "8" as 20:00 delivers the coffee
    reminder at night.

    ⚠️ No ``dtstart``. RFC 5545 needs one and the resolver requires it, but it is not stored here
    because it is not a new fact: it is the owning fact's ``created_at``, the day the user told us.
    Anchoring it anywhere else changes what the rule *means*
    (see :func:`avid.core.schedule.next_occurrence`).
    """

    rrule: str  # RFC 5545, e.g. 'FREQ=DAILY'
    local_time: str  # 'HH:MM' wall clock, in `timezone`
    timezone: str  # IANA, e.g. 'Asia/Beirut'
    lead_time_s: int = 300  # §8.3's default: an 08:00 routine fires at 07:55


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
    """The four §7.7 weights (α, β, γ, δ). Defaults are Park et al.'s **equal-weight baseline**
    (α=β=γ=1) — *"deviating from a published baseline before measuring is how you end up
    tuning noise."* The service overrides these from ``WeightsConfig`` (P7); the defaults keep
    the domain function callable and testable without config."""

    recency: float = 1.0
    importance: float = 1.0
    relevance: float = 1.0
    # δ, the keyword term (#264), MEASURED at #447 — see ScoreWeights.keyword below.
    # Park et al. have no keyword component, so unlike the three
    # above this default is NOT a published baseline — it is the same equal weight applied for
    # consistency, and it is **unmeasured**. `tools/eval_recall.py` is the instrument that would
    # settle it; until it has been run against a real-MiniLM store, treat 1.0 as a starting point
    # rather than a result. Said plainly here because an invented number that looks like the
    # three beside it is how a guess becomes a fact.
    keyword: float = 0.5


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
    # Did FTS5 match the query text against this fact (§7.7's proper-noun branch)? #264.
    # ⚠️ **Deliberately has no default.** Before #264 the keyword branch only widened the
    # candidate set, and at any store smaller than the vector pool (50) that union was a no-op —
    # so the branch §7.7 rests its proper-noun argument on could not change a single result. A
    # default here would let a future caller silently reintroduce exactly that: scoring that
    # compiles, runs, and quietly ignores the keyword evidence. Making it required forces the
    # decision to be visible at every construction site.
    keyword_hit: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ScoredCandidate:
    """A candidate with its combined §7.7 score. :func:`rank_candidates` returns these
    best-first, truncated to k."""

    fact_id: int
    score: float


@dataclass(frozen=True, slots=True, kw_only=True)
class RetrievalMatch:
    """A retrieval hit with the **evidence** for it, rather than only its rank (#257).

    Distinct from :class:`ScoredCandidate` for one reason: ``score`` there is the §7.7 blend
    *after* :func:`_min_max` normalises each component across the candidate set, so it is a
    ranking, not a measurement — a lone candidate always scores 1.0. A caller that needs to
    decide *whether a match is good enough at all* cannot use it, and `forget` is exactly such a
    caller: it deletes irreversibly (§7.10), so it must see the raw evidence.

    ``relevance`` is therefore the **unnormalised** cosine straight from the index, and
    ``keyword_hit`` records whether FTS5 matched the query text directly. Both are needed because
    neither alone is sufficient: vectors are weak on proper nouns (the reason §7.7 has an FTS5
    branch at all), and a keyword hit says nothing about semantic closeness.
    """

    fact_id: int
    relevance: (
        float  # raw cosine, NOT min-max normalised — 0.0 when the fact has no embedding
    )
    keyword_hit: bool  # FTS5 matched the query text against this fact


def deletable_ids(
    matches: Sequence[RetrievalMatch], *, floor: float
) -> tuple[int, ...]:
    """The ids `forget` may delete: cosine at or above ``floor``, **or** a direct FTS5 hit (#257).

    Pure policy, unit-tested, deliberately not in the adapter: *what is close enough to destroy*
    is an application decision, and the index's job is to report facts rather than to decide them.

    The rule is a disjunction because the two signals fail in opposite places. Without the floor,
    `forget` deletes whatever the top-k happened to contain — on the M7 gate one call destroyed
    five of six facts, matching at cosines a human would never call a match. Without the keyword
    branch, forgetting by name breaks: MiniLM scores *"Ali prefers tea"* against a coffee fact at
    0.57, and a proper noun like "Biscuit" embeds to something generic, which is precisely why
    §7.7 unions FTS5 into retrieval in the first place.

    Order is preserved from ``matches`` (the retriever hands them over best-first), so a caller
    that also caps the count deletes the strongest matches rather than an arbitrary subset.
    """
    return tuple(m.fact_id for m in matches if m.relevance >= floor or m.keyword_hit)


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

    ``score = α·recency + β·importance + γ·relevance + δ·keyword``, each component min-max
    normalised across *this candidate set* to [0,1] (:func:`_min_max`) before the weighted sum.
    Pure — ``weights``, ``half_life_days`` and ``k`` are all injected (from config; §7.7, P7),
    so the domain hard-codes none of them (AC-5).

    **δ·keyword is #264's fix, and it is what makes §7.7's hybrid claim true.** §7.7 argues that
    vector search alone fails on proper nouns — *"Maya embeds to mush"* — and answers it by
    unioning the vector pool with an FTS5 keyword search. That fixed **candidate generation**,
    but candidate generation was never the binding constraint: with fewer live facts than the
    retriever's vector pool (50), every fact is already a candidate and the union changes
    nothing. Ranking was the constraint, and it had no keyword term — so a proper-noun fact with
    a mushy embedding lost on recency/importance exactly as if FTS5 did not exist. Proven by
    removing the keyword branch entirely and observing byte-identical results at 3 and 18 facts.

    Normalising a 0/1 indicator is the identity whenever the set contains both hits and misses —
    the only case where the term carries information. When every candidate is a hit (or none is),
    :func:`_min_max`'s degenerate rule gives them all ``1.0``, adding a constant that cannot
    reorder anything. So the component is well behaved without being special-cased, and the
    "every component is normalised" rule stays true without exception.

    Determinism (AC-5): candidates are ordered by ``(-score, fact_id)`` — highest score first,
    ascending ``fact_id`` breaking ties, so the result never depends on input or dict ordering.
    An empty candidate list returns ``()``.
    """
    if not candidates:
        return ()
    recency = _min_max([recency_decay(c.age_days, half_life_days) for c in candidates])
    importance = _min_max([float(c.importance) for c in candidates])
    relevance = _min_max([c.relevance for c in candidates])
    keyword = _min_max([1.0 if c.keyword_hit else 0.0 for c in candidates])
    scored = [
        ScoredCandidate(
            fact_id=c.fact_id,
            score=(
                weights.recency * recency[i]
                + weights.importance * importance[i]
                + weights.relevance * relevance[i]
                + weights.keyword * keyword[i]
            ),
        )
        for i, c in enumerate(candidates)
    ]
    scored.sort(key=lambda s: (-s.score, s.fact_id))
    return tuple(scored[:k])


# ── §6.7 pre-injection selection — the top_facts block MemoryService injects at session open. Pure:
# the caller passes the live facts (already recency-ordered from fetch_live); no clock, no I/O. ──

# The set of always-included kinds (§6.7): "who the user is" (identity) and "what they do" (routine),
# the durable facts UC-01/02/03 turn on — taken ahead of everything else, in the caller's recency order.
_PRE_INJECT_KINDS: frozenset[FactKind] = frozenset({"identity", "routine"})

# ~4 characters per token, the OpenAI rule of thumb (§6.7). The budget is a soft ceiling on a cached
# instruction prefix, not a billing figure, so an estimate is all that is needed.
_CHARS_PER_TOKEN = 4


def _estimate_tokens(text: str) -> int:
    """Rough token count for a fact's text (~4 chars/token, §6.7) — at least 1 for any non-empty fact."""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def select_top_facts(
    facts: Sequence[Fact], *, max_facts: int, max_tokens: int
) -> tuple[Fact, ...]:
    """Choose the §6.7 pre-session injection set: identity + active routines + recent high-importance.

    The ~10–15-fact / ~600-token block that lands in instruction layer 4 before a session opens, so it
    is cached prefix (§6.4) and must stay bounded by **both** a count and a token estimate — unbounded
    growth silently inflates every turn's cost. Pure and deterministic: ``facts`` are the live facts,
    already ``last_accessed_at``-descending from :meth:`~avid.core.ports.FactRepository.fetch_live`; this
    reads no clock and does no I/O.

    Priority: every ``identity`` fact and every live ``routine`` first (in the caller's recency order),
    then the remaining facts by importance, recency breaking ties (a stable sort over the recency-ordered
    input). Selection stops at whichever bound binds first; the first fact is always admitted even if it
    alone exceeds ``max_tokens`` — an empty injection block would be worse than a slightly over-budget one.
    """
    must = [f for f in facts if f.kind in _PRE_INJECT_KINDS]
    rest = sorted(
        (f for f in facts if f.kind not in _PRE_INJECT_KINDS),
        key=lambda f: (
            -f.importance
        ),  # stable: equal importance keeps the input's recency order
    )
    chosen: list[Fact] = []
    tokens = 0
    for fact in (*must, *rest):
        if len(chosen) >= max_facts:
            break
        cost = _estimate_tokens(fact.text)
        if chosen and tokens + cost > max_tokens:
            break
        chosen.append(fact)
        tokens += cost
    return tuple(chosen)
