"""``MemoryService`` — the sole writer and reader of persistent user knowledge (#122, SDS §3.6.1).

Every other service is bus-shaped: subscribe, call ports, publish. This one is the **documented
exception** (§3.5, §9.1.4). *"Is at-most-once delivery acceptable? For everything except memory writes,
yes … For memory writes it is not acceptable, and therefore memory writes do not go through the bus."*
So the four public methods here are **direct awaited calls, durable before they return**, that *then*
publish a ``memory.*`` fact for whoever cares (that publish is how UC-02 becomes UC-03 with zero
coupling — ``BehaviorService`` subscribes to ``memory.fact_stored`` and this service never learns the
behaviour engine exists). ``subscriptions()`` is therefore **empty** (§3.6.1): nothing reaches this
service over the bus; it is reached by call.

It depends only on **Protocols** (P2) — ``EventBus``/``Clock``/``FactRepository``/``Embedder``/
``TextModel``/``Retriever`` — and is constructed only by the composition root (P3). It owns the
store + index **lifecycle** the read-path adapters deferred: ``start`` runs the boot rebuild (§8.5),
``stop`` closes the store. The blocking SQLite and any model inference ride the adapters' own threads,
so nothing here touches the loop (P8).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from uuid import UUID, uuid4

from avid.core.embedding import pack_embedding
from avid.core.envelope import envelope
from avid.core.event_bus import Subscription
from avid.core.ports import (
    Clock,
    Embedder,
    EventBus,
    FactRepository,
    Retriever,
    TextModel,
)
from avid.domain import (
    Fact,
    MemoryFactDeleted,
    MemoryFactStored,
    MemoryFactSuperseded,
    select_top_facts,
)

_log = logging.getLogger(__name__)

# The §9.1.3 catalog name stamped on the memory.* facts this service publishes (`memory.fact_stored`
# / `_superseded` / `_deleted`). `memory.recall_completed` is published by the Retriever adapter it
# delegates to (#120), so that one carries the retriever's source, not this.
_SOURCE = "MemoryService"


class MemoryService:
    """Store, retrieve, forget, and pre-inject persistent facts (SDS §7, §9.2).

    Satisfies the :class:`~avid.core.ports.Service` shape (``name``/``start``/``stop``/
    ``subscriptions``). Not reactive but not task-free either: ``start`` rebuilds the §8.5 index from the
    store, so the composition root hands it to the lifecycle like ``AudioService`` — its liveness is the
    rebuild, and ``stop`` closes the store within the §9.2 budget.
    """

    name = _SOURCE

    def __init__(
        self,
        *,
        bus: EventBus,
        clock: Clock,
        repo: FactRepository,
        retriever: Retriever,
        embedder: Embedder,
        text_model: TextModel,
        supersession_threshold: float,
        supersession_k: int,
        top_facts_max: int,
        top_facts_token_budget: int,
    ) -> None:
        self._bus = bus
        self._clock = clock
        self._repo = repo
        self._retriever = retriever
        self._embedder = embedder
        self._text_model = text_model
        self._supersession_threshold = supersession_threshold
        self._supersession_k = supersession_k
        self._top_facts_max = top_facts_max
        self._top_facts_token_budget = top_facts_token_budget

    # --- SDS §9.2 service shape ----------------------------------------------------------

    async def start(self) -> None:
        """Boot (§8.5, AC-6): rebuild the in-memory index from the store — SQLite is truth, the matrix
        is a write-through cache reconstructed on start. The store's migrations run lazily on its writer
        thread the first time ``rebuild`` reads it, so this one call reconciles a fresh, an existing, and
        a disagreeing database alike."""
        await self._retriever.rebuild()
        _log.info("memory ready: index rebuilt from the store")

    async def stop(self) -> None:
        """Close the store (idempotent) within the §9.2 5 s budget. The index is in-memory — nothing to
        flush; SQLite is already durable after every write."""
        await self._repo.aclose()

    def subscriptions(self) -> Sequence[Subscription]:
        """**Empty by design** (§3.6.1, AC-3): ``MemoryService`` subscribes to nothing — it is reached
        by direct call, never over the bus. Stated as code, and pinned by a test, so it is a decision
        rather than an accident of an unwritten method."""
        return ()

    # --- the §9.1.4 direct-call surface --------------------------------------------------

    async def store_fact(
        self, fact: Fact, *, correlation_id: UUID | None = None
    ) -> int:
        """Store ``fact`` durably, resolving §7.8 supersession first, then publish (AC-2). Returns its id.

        The full §7.8 write: embed the fact once, ask the index for near-duplicates (cosine ≥ the
        supersession threshold), and — if any — let the :class:`~avid.core.ports.TextModel` judge which
        the new fact updates or contradicts. The new fact is inserted (durable), each superseded fact is
        pointed at it and dropped from the live index, and ``memory.fact_superseded`` /
        ``memory.fact_stored`` are published **after** the writes commit — the notification always
        follows the fact (§3.7.3). ``source_correlation_id`` is stamped on every stored fact so it traces
        back to the turn that produced it (§3.12.2); ``created_at`` / ``last_accessed_at`` default to now
        when the caller left them unset.
        """
        vector = await self._embedder.embed(fact.text)
        blob = pack_embedding(vector)
        superseded_ids = await self._resolve_supersession(fact.text, vector)

        corr = correlation_id or fact.source_correlation_id or uuid4()
        now = self._clock.now()
        to_store = replace(
            fact,
            source_correlation_id=corr,
            created_at=fact.created_at or now,
            last_accessed_at=fact.last_accessed_at or now,
        )
        new_id = await self._repo.add(to_store, embedding=blob)

        for old_id in superseded_ids:
            await self._repo.mark_superseded(old_id, new_id, at=now)
            self._retriever.remove(old_id)
            await self._bus.publish(
                MemoryFactSuperseded(
                    **envelope(clock=self._clock, correlation_id=corr, source=_SOURCE),
                    old_id=old_id,
                    new_id=new_id,
                )
            )

        stored = replace(to_store, id=new_id)
        self._retriever.append(stored, blob)
        await self._bus.publish(
            MemoryFactStored(
                **envelope(clock=self._clock, correlation_id=corr, source=_SOURCE),
                fact_id=new_id,
                kind=stored.kind,
                importance=stored.importance,
            )
        )
        _log.info(
            "stored fact %d (kind=%s, importance=%d), superseding %d",
            new_id,
            stored.kind,
            stored.importance,
            len(superseded_ids),
        )
        return new_id

    async def retrieve(
        self, query: str, *, correlation_id: UUID | None = None
    ) -> Sequence[Fact]:
        """The `recall` tool's engine (§6.7 path 2): the top-k live facts for ``query``, best first.

        Delegates the ranking to the :class:`~avid.core.ports.Retriever` (which publishes
        ``memory.recall_completed`` itself, so this must not re-publish) and hydrates the returned ids to
        full :class:`~avid.domain.Fact` values for the caller. A read — no access-count/recency bump
        here (§7.9 is Phase-10 work)."""
        ids = await self._retriever.retrieve(query, correlation_id=correlation_id)
        return await self._hydrate(ids)

    async def top_facts(self) -> Sequence[Fact]:
        """Pre-session injection (§6.7 path 1, AC-4): identity + active routines + recent high-importance,
        bounded by count **and** a token estimate. A pure read over the live facts — publishes nothing
        (there is no catalogued event for it); the caller injects the result into instruction layer 4."""
        live = await self._repo.fetch_live()
        return select_top_facts(
            live,
            max_facts=self._top_facts_max,
            max_tokens=self._top_facts_token_budget,
        )

    async def forget(self, query: str, *, correlation_id: UUID | None = None) -> int:
        """The `forget` tool (§7.10, UC-07, AC-5): hard-delete the facts matching ``query``. Returns the
        count deleted.

        A **rights** operation, not supersession: each matched fact is removed from **SQLite first**
        (cascading to its embedding/routines/episodes via ``ON DELETE CASCADE``), **then from the
        matrix** (§8.5's ordering — so a retrieval can never surface a fact whose row is already gone),
        then ``memory.fact_deleted`` is published. Losing a deletion is a privacy bug (§3.7.3), so every
        step is awaited and durable before this returns."""
        ids = await self._retriever.retrieve(query, correlation_id=correlation_id)
        corr = correlation_id or uuid4()
        for fact_id in ids:
            await self._repo.delete(fact_id)
            self._retriever.remove(fact_id)
            await self._bus.publish(
                MemoryFactDeleted(
                    **envelope(clock=self._clock, correlation_id=corr, source=_SOURCE),
                    fact_id=fact_id,
                )
            )
        _log.info("forgot %d fact(s) matching %r", len(ids), query)
        return len(ids)

    # --- helpers -------------------------------------------------------------------------

    async def _resolve_supersession(
        self, new_text: str, vector: Sequence[float]
    ) -> Sequence[int]:
        """§7.8 steps 2–3: the near-duplicate ids the new fact supersedes, or ``()`` if none.

        Only fires the (cheap, off-turn-path) text-model call when the index actually holds a
        near-duplicate — the common write has no candidates and skips it entirely."""
        candidate_ids = await self._retriever.similar(
            vector, threshold=self._supersession_threshold, k=self._supersession_k
        )
        if not candidate_ids:
            return ()
        candidates: list[tuple[int, str]] = []
        for cid in candidate_ids:
            existing = await self._repo.get(cid)
            if existing is not None:
                candidates.append((cid, existing.text))
        if not candidates:
            return ()
        return await self._text_model.judge_supersession(
            new_fact=new_text, candidates=candidates
        )

    async def _hydrate(self, ids: Sequence[int]) -> Sequence[Fact]:
        """Load full facts for ``ids``, dropping any that vanished between ranking and read (a concurrent
        ``forget``). Preserves the retriever's best-first order."""
        facts: list[Fact] = []
        for fact_id in ids:
            fact = await self._repo.get(fact_id)
            if fact is not None:
                facts.append(fact)
        return facts


__all__ = ["MemoryService"]
