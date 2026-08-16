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
from avid.core.schedule import Routine, next_occurrence
from avid.domain import (
    FACT_KINDS,
    Fact,
    MemoryFactDeleted,
    MemoryFactStored,
    MemoryFactSuperseded,
    RoutineSpec,
    deletable_ids,
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
        forget_relevance_floor: float,
        forget_k: int,
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
        self._forget_relevance_floor = forget_relevance_floor
        self._forget_k = forget_k
        self._top_facts_max = top_facts_max
        self._top_facts_token_budget = top_facts_token_budget
        # How many `routine`-kind facts arrived with no schedule this session (§6.6, M10). A plain
        # attribute rather than an event, like ExpressionService's staleness counters: the point is
        # that the number exists at all. A zero from an absent instrument and a real zero read
        # identically, and #310 cost six live runs to that exact confusion.
        self.routines_without_schedule = 0

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
        self,
        fact: Fact,
        *,
        routine: RoutineSpec | None = None,
        correlation_id: UUID | None = None,
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
        corr = correlation_id or fact.source_correlation_id or uuid4()
        vector = await self._embedder.embed(fact.text)
        blob = pack_embedding(vector)
        superseded_ids = await self._resolve_supersession(fact.text, vector, corr)

        now = self._clock.now()
        to_store = replace(
            fact,
            source_correlation_id=corr,
            created_at=fact.created_at or now,
            last_accessed_at=fact.last_accessed_at or now,
        )
        new_id = await self._repo.add(to_store, embedding=blob, routine=routine)

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

    # Importance is LLM-rated 1–10 at write time (§7.6) and the §8.3 CHECK enforces the range;
    # we reject out-of-range here so a bad tool argument becomes an honest tool error, not a
    # write that the database rejects only at COMMIT.
    _IMPORTANCE_MIN = 1
    _IMPORTANCE_MAX = 10

    async def remember_fact(
        self,
        text: str,
        kind: str,
        importance: int,
        *,
        schedule: RoutineSpec | None = None,
        correlation_id: UUID | None = None,
    ) -> int:
        """The `remember_fact` tool (§7.6, UC-02, AC-3): build a :class:`~avid.domain.Fact` from the
        model's extraction and store it durably, returning its id.

        The model owns *what* to remember (ADR-004); this owns the trivial turn→``Fact`` construction
        the tool arguments imply and then defers to :meth:`store_fact` for the full §7.8 write
        (embed → supersession judge → insert → publish), so it is durable before it returns. ``kind``
        and ``importance`` are validated against the §8.3 constraints **before** any write — an
        invalid one raises :class:`ValueError` (the dispatcher turns that into a tool error, AC-6)
        rather than reaching the store and failing at COMMIT. Timestamps and confidence take the
        :class:`~avid.domain.Fact` defaults inside ``store_fact``."""
        if kind not in FACT_KINDS:
            raise ValueError(
                f"unknown fact kind {kind!r} — must be one of {list(FACT_KINDS)} (§8.3)"
            )
        if not self._IMPORTANCE_MIN <= importance <= self._IMPORTANCE_MAX:
            raise ValueError(
                f"importance {importance} out of range "
                f"{self._IMPORTANCE_MIN}–{self._IMPORTANCE_MAX} (§7.6)"
            )
        if schedule is not None:
            self._validate_schedule(schedule)
        elif kind == "routine":
            # ⚠️ A routine fact with no schedule is a legitimate outcome — not every routine has a
            # clock time — but it is indistinguishable from a model that has quietly stopped
            # filling the field, and that is exactly the shape of #310: `set_affect` dispatched
            # perfectly against every fake and fired zero times in six live runs, because nothing
            # counted the difference between "no calls" and "no instrument". Counted here, loudly.
            self.routines_without_schedule += 1
            _log.warning(
                "routine fact stored with no schedule (%r) — it will never fire; "
                "%d so far this session [correlation_id=%s]",
                text,
                self.routines_without_schedule,
                correlation_id,
            )
        fact = Fact(
            id=0,  # assigned by the store on insert (§8.2); ignored here
            text=text,
            kind=kind,  # mypy narrows to FactKind via the FACT_KINDS guard above
            importance=importance,
            created_at=0,  # store_fact defaults unset timestamps to now
            last_accessed_at=0,
        )
        return await self.store_fact(
            fact, routine=schedule, correlation_id=correlation_id
        )

    def _validate_schedule(self, schedule: RoutineSpec) -> None:
        """Resolve the schedule once, now, so an unusable one never reaches the database.

        The resolver is the authority on what is usable — a bad RRULE, a malformed ``HH:MM`` or an
        unknown IANA zone all raise :class:`~avid.core.schedule.InvalidRoutine`, which is a
        ``ValueError`` and therefore already the shape the tool dispatcher turns into a tool error
        (AC-6). Re-checking any of it here would be a second, drifting copy of §10.3's rules.
        """
        probe = Routine(
            rrule=schedule.rrule,
            local_time=schedule.local_time,
            timezone=schedule.timezone,
            dtstart_epoch=self._clock.now(),
            lead_time_s=schedule.lead_time_s,
        )
        next_occurrence(probe, after=self._clock.now())

    async def recall(
        self, query: str, *, k: int = 5, correlation_id: UUID | None = None
    ) -> Sequence[Fact]:
        """The `recall` tool (§6.7 path 2, UC-05): the top facts for ``query``, best first, capped at
        ``k``. Delegates ranking to :meth:`retrieve` (whose retriever already caps at the configured
        ``top_k`` and publishes ``memory.recall_completed``); ``k`` is the model-requested cutoff, so
        the result is truncated to it in case the model asks for fewer than ``top_k``."""
        return (await self.retrieve(query, correlation_id=correlation_id))[:k]

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
        step is awaited and durable before this returns.

        **What it may delete is bounded (#257).** It asks the retriever for *evidence* rather than a
        ranking and applies :func:`~avid.domain.deletable_ids`: cosine at or above
        ``forget_relevance_floor``, or a direct FTS5 hit. Until the M7 gate this deleted every id the
        top-k returned — one call destroyed five of six facts, at cosines no one would call a match —
        and because the DELETE cascades there is nothing to undo it with. `retrieve` is deliberately
        **not** used here: it ranks (min-max normalised, so a lone candidate always looks perfect)
        and it publishes ``memory.recall_completed``, which a deletion is not."""
        matches = await self._retriever.match(query, k=self._forget_k)
        ids = deletable_ids(matches, floor=self._forget_relevance_floor)
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
        self, new_text: str, vector: Sequence[float], correlation_id: UUID
    ) -> Sequence[int]:
        """§7.8 steps 2–3: the near-duplicate ids the new fact supersedes, or ``()`` if none.

        Only fires the (cheap, off-turn-path) text-model call when the index actually holds a
        near-duplicate — the common write has no candidates and skips it entirely.

        **AC-9 graceful degradation:** the real :class:`~avid.core.ports.TextModel` can time out, error,
        or return nonsense — and this runs inside a tool handler, where a raise would abandon the whole
        write. So a judge failure is caught, logged with the turn's ``correlation_id`` (§3.12.2), and read
        as *no supersession*: the fact is still stored (the caller inserts it regardless), history is
        simply not rewritten this time. Never losing the write, never crashing, is the invariant (§3.7.3);
        the ``FakeTextModel`` never raises, so this guard exists for the ``openai`` adapter (#121)."""
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
        try:
            return await self._text_model.judge_supersession(
                new_fact=new_text, candidates=candidates
            )
        except Exception:
            _log.warning(
                "supersession judge failed [%s] — storing the fact without supersession",
                correlation_id,
                exc_info=True,
            )
            return ()

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
