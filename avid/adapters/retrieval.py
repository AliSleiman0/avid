"""The hybrid retriever + its §8.5 in-memory vector index (#120).

The M7 **read path** — the piece that turns stored facts + a query into ranked recall, and the
one R-07 ("retrieval feels senile") lives or dies on. Two ideas from §7.7/§8.5 meet here:

* **The in-memory index (§8.5).** The schema is the durable store; it is *not* the query path.
  Deserialising N embedding BLOBs per query would make retrieval O(N) in *Python* — a far worse
  constant than O(N) in ``numpy`` — so the live vectors sit in a float32 ``N×384`` matrix and a
  query is one matmul. The matrix is **write-through** (:meth:`append`/:meth:`remove`) and
  **rebuilt from SQLite on boot** (:meth:`rebuild`). The invariant is stated once and enforced by
  test: *SQLite is truth, the matrix is an index* — any divergence is a bug the boot rebuild
  reconciles.
* **Hybrid retrieval (§7.7).** Vector search alone fails on proper nouns — *"Maya" embeds to
  mush* — so candidates are the **∪** of the vector top-pool and an FTS5 keyword search
  (:meth:`FactRepository.keyword_search`), merged, then handed to #116's pure
  :func:`~avid.domain.rank_candidates` for the ``α·recency + β·importance + γ·relevance`` score.

Like ``HealthServer``, this is a **portless** concrete adapter (there is no ``Retriever``
Protocol): it is constructed only by the composition root (P3) and depends on the
``FactRepository`` / ``Embedder`` / ``EventBus`` / ``Clock`` **Protocols** (P2), never concretes.
``numpy`` is **lazy-imported inside the methods that need it** (exactly as ``SileroVad`` imports
``onnxruntime``), so this module still imports on a host without the ``memory`` extra and ``core``
/ ``domain`` stay ``numpy``-free (P1, ADR-012). The matmul runs **inline**: at the low-thousands
scale a single-user robot ever reaches it is sub-millisecond, well under the 50 ms slow-callback
gate (P8) — no executor, like ``SileroVad``'s inference.

Its ``rebuild()`` / ``aclose`` lifecycle and its wiring into ``MemoryService`` land with #122;
#120 delivers the retriever, its contract-fed collaborators, and the real recall@5 eval number.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

from avid.core.embedding import EMBEDDING_DTYPE
from avid.core.envelope import envelope
from avid.core.ports import Clock, Embedder, EventBus, FactRepository
from avid.domain import (
    Fact,
    MemoryRecallCompleted,
    RetrievalCandidate,
    RetrievalMatch,
    ScoreWeights,
    rank_candidates,
)

_log = logging.getLogger("avid.adapters.retrieval")

# The §9.1.3 catalog name an operator reads to know who published a recall.
_SOURCE = "HybridRetriever"

# How many facts the *vector* side contributes to the candidate union before §7.7 scoring narrows
# to top_k. Larger than top_k so a fact the keyword branch would miss still gets a fair score;
# small enough that the union stays tiny. Not a config knob — unlike top_k/weights/half-life
# (AC-5), the issue does not mandate tuning it, and at low-thousands scale it barely bites.
_VECTOR_POOL = 50

# 86,400 seconds per day — Fact timestamps are epoch **seconds** (§8.2), age is in days (§7.7).
_SECONDS_PER_DAY = 86_400.0


def _stack(blobs: Sequence[bytes]) -> Any:
    """Stack packed §8.2 embeddings into one ``(N, 384)`` float32 matrix, or ``None`` when empty.

    Runs in a worker thread (:meth:`HybridRetriever.rebuild`), so both numpy's one-time import and
    the vstack over thousands of BLOBs stay off the event loop (P8). numpy is imported first even on
    an empty store, so it is warm for the first inline :meth:`~HybridRetriever.retrieve` matmul."""
    import numpy as np

    if not blobs:
        return None
    return np.vstack([np.frombuffer(b, dtype=EMBEDDING_DTYPE) for b in blobs]).astype(
        np.float32
    )


class HybridRetriever:
    """FTS5 ∪ cosine retrieval over a write-through in-memory vector index (§7.7, §8.5)."""

    def __init__(
        self,
        *,
        repo: FactRepository,
        embedder: Embedder,
        bus: EventBus,
        clock: Clock,
        top_k: int,
        half_life_days: float,
        weights: ScoreWeights,
        vector_pool: int = _VECTOR_POOL,
    ) -> None:
        self._repo = repo
        self._embedder = embedder
        self._bus = bus
        self._clock = clock
        self._top_k = top_k
        self._half_life_days = half_life_days
        self._weights = weights
        self._vector_pool = vector_pool
        # "The index": the write-through vector matrix (embedded live facts) plus per-fact scoring
        # metadata for *every* live fact — so a keyword-only hit with no embedding is still
        # scorable. Both are rebuilt from SQLite on boot; a candidate the index has never heard of
        # (a fact inserted behind its back) is dropped until the next rebuild reconciles it.
        self._ids: list[int] = []  # row order: matrix row i ↔ fact self._ids[i]
        self._row_of: dict[int, int] = {}  # fact id → matrix row index
        self._matrix: Any = None  # numpy (N, D) float32, or None when empty
        self._meta: dict[
            int, tuple[int, int]
        ] = {}  # id → (importance, last_accessed_at)

    async def rebuild(self) -> None:
        """Reconcile the index from SQLite (§8.5) — the boot rebuild, and the only reconciliation.

        Loads scoring metadata for every live fact and the matrix from every live fact that has an
        embedding. Called at boot (and after any out-of-band DB mutation) by #122; retrieval never
        touches the DB for vectors, only this matrix. Stacking thousands of BLOBs into the matrix —
        and numpy's one-time import — is real CPU that runs **off the loop** in a thread (P8), like
        the repo's own I/O; only the (trivial) assignment happens back on the loop."""
        facts = await self._repo.fetch_live()
        pairs = await self._repo.load_embeddings()
        self._meta = {f.id: (f.importance, f.last_accessed_at) for f in facts}
        self._ids = [fid for fid, _ in pairs]
        self._row_of = {fid: i for i, fid in enumerate(self._ids)}
        self._matrix = await asyncio.to_thread(_stack, [blob for _, blob in pairs])
        _log.debug(
            "index rebuilt: %d live facts, %d vectors", len(self._meta), len(self._ids)
        )

    def append(self, fact: Fact, embedding: bytes | None) -> None:
        """Write-through: reflect a just-stored fact in the index (§8.5), after its row is durable.

        Records the fact's scoring metadata and, if it carries a vector, appends a matrix row.
        Called by #122's ``store_fact`` once the DB insert has committed — never before, so the
        index never advertises a fact SQLite has not yet accepted."""
        self._meta[fact.id] = (fact.importance, fact.last_accessed_at)
        if embedding is None:
            return
        import numpy as np

        row = (
            np.frombuffer(embedding, dtype=EMBEDDING_DTYPE)
            .astype(np.float32)
            .reshape(1, -1)
        )
        self._matrix = row if self._matrix is None else np.vstack([self._matrix, row])
        self._row_of[fact.id] = len(self._ids)
        self._ids.append(fact.id)

    def remove(self, fact_id: int) -> None:
        """Write-through: drop a fact from the index (§8.5), the matrix half of #122's ``forget``.

        Removes the scoring metadata and, if present, the matrix row (re-indexing the rows after
        it). ``forget`` removes from SQLite first, then calls this — so a retrieval can never
        surface a fact whose row has already gone."""
        self._meta.pop(fact_id, None)
        if fact_id not in self._row_of:
            return
        import numpy as np

        r = self._row_of[fact_id]
        self._ids.pop(r)
        self._matrix = np.delete(self._matrix, r, axis=0)
        if self._matrix.shape[0] == 0:
            self._matrix = None
        self._row_of = {fid: i for i, fid in enumerate(self._ids)}

    async def similar(
        self, vector: Sequence[float], *, threshold: float, k: int
    ) -> tuple[int, ...]:
        """Near-duplicate search: live fact ids with cosine ≥ ``threshold``, best first, capped at ``k``.

        The §7.8 supersession pre-check — a write embeds the new fact, then asks the index which existing
        facts are near-enough duplicates to be candidates for contradiction. One matmul over
        pre-normalised vectors (cosine *is* the dot product, §8.2), filtered by ``threshold`` and
        truncated to ``k`` by descending cosine. Publishes nothing (unlike :meth:`retrieve`, this is not
        a recall). Returns ``()`` when the index holds no vectors."""
        if self._matrix is None:
            return ()
        import numpy as np

        q = np.asarray(vector, dtype=np.float32)
        scores = self._matrix @ q  # (N,) cosines — both operands are unit vectors
        hits: list[int] = []
        for r in np.argsort(-scores)[
            :k
        ]:  # descending, so the first sub-threshold ends it
            if float(scores[int(r)]) < threshold:
                break
            hits.append(self._ids[int(r)])
        return tuple(hits)

    async def retrieve(
        self, query: str, *, correlation_id: UUID | None = None
    ) -> tuple[int, ...]:
        """Hybrid retrieve: the top-k live fact ids for ``query``, best first (§7.7).

        Embeds the query, scores every indexed vector in **one matmul** over pre-normalised vectors
        (cosine *is* the dot product, §8.2 — no per-query normalisation), unions the vector top-pool
        with an FTS5 keyword search (the proper-noun branch), and hands the merged candidates to
        #116's :func:`~avid.domain.rank_candidates`. Publishes ``memory.recall_completed`` with a
        ``latency_ms`` derived from ``monotonic_ns`` (never wall clock, §9.1.1). ``correlation_id``
        is the turn this recall serves (#122 / the ``recall`` tool pass it); a fresh id is minted
        when a recall stands alone (the eval harness)."""
        started_ns = self._clock.monotonic_ns()
        candidates, _ = await self._candidates(query)
        ranked = rank_candidates(
            candidates,
            weights=self._weights,
            half_life_days=self._half_life_days,
            k=self._top_k,
        )
        result = tuple(s.fact_id for s in ranked)

        latency_ms = (self._clock.monotonic_ns() - started_ns) / 1_000_000
        await self._bus.publish(
            MemoryRecallCompleted(
                **envelope(
                    clock=self._clock,
                    correlation_id=correlation_id or uuid4(),
                    source=_SOURCE,
                ),
                query=query,
                n_returned=len(result),
                latency_ms=latency_ms,
            )
        )
        return result

    async def match(self, query: str, *, k: int) -> tuple[RetrievalMatch, ...]:
        """The hybrid candidates for ``query`` with the **evidence** for each (#257, §7.7).

        Same candidate generation as :meth:`retrieve` — one matmul over the §8.5 matrix, unioned
        with FTS5's keyword hits — but it hands back the **raw** cosine and the keyword flag instead
        of a ranking, because §7.7's combined score is min-max normalised across the candidate set
        and so cannot say whether a match is good in absolute terms. ``forget`` needs that, since it
        deletes irreversibly (§7.10); the policy itself lives in the domain
        (:func:`~avid.domain.deletable_ids`), not here.

        Ordered by raw cosine, best first, truncated to ``k``. **Publishes nothing** — a forget is
        not a recall (cf. :meth:`similar`)."""
        candidates, keyword_ids = await self._candidates(query)
        matches = [
            RetrievalMatch(
                fact_id=c.fact_id,
                relevance=c.relevance,
                keyword_hit=c.fact_id in keyword_ids,
            )
            for c in candidates
        ]
        # Sorted on the raw cosine, with fact_id breaking ties, so the order is deterministic and
        # does not depend on set iteration — the same determinism rule rank_candidates states.
        matches.sort(key=lambda m: (-m.relevance, m.fact_id))
        return tuple(matches[:k])

    async def _candidates(
        self, query: str
    ) -> tuple[list[RetrievalCandidate], frozenset[int]]:
        """The shared candidate generation behind :meth:`retrieve` and :meth:`match`.

        Extracted so the two cannot drift: a `forget` that considered a different candidate set than
        a `recall` would make "it never comes back from retrieval" untestable. Returns the scoring
        candidates (carrying the raw cosine in ``relevance``) and the FTS5 hit ids.
        """
        import numpy as np

        vector = await self._embedder.embed(query)
        keyword_ids = await self._repo.keyword_search(query, limit=self._top_k)

        scores: Any = None
        candidate_ids: set[int] = set()
        if self._matrix is not None:
            q = np.asarray(vector, dtype=np.float32)
            scores = self._matrix @ q  # (N,) cosines — both operands are unit vectors
            pool = min(self._vector_pool, len(self._ids))
            for r in np.argsort(-scores)[:pool]:
                candidate_ids.add(self._ids[int(r)])
        # Union the keyword hits, but only ones the index knows how to score — a hit inserted
        # behind the index's back has no metadata and waits for the next rebuild (the §8.5 invariant).
        candidate_ids.update(fid for fid in keyword_ids if fid in self._meta)

        now = self._clock.now()
        candidates = [
            RetrievalCandidate(
                fact_id=fid,
                importance=self._meta[fid][0],
                age_days=max(0.0, (now - self._meta[fid][1]) / _SECONDS_PER_DAY),
                relevance=(
                    float(scores[self._row_of[fid]])
                    if scores is not None and fid in self._row_of
                    else 0.0
                ),
            )
            for fid in candidate_ids
        ]
        return candidates, frozenset(fid for fid in keyword_ids if fid in self._meta)
