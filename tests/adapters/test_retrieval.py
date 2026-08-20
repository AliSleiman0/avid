"""HybridRetriever — the §7.7/§8.5 memory read path (#120).

Driven against the P6 fakes (:class:`FakeFactRepository`, :class:`FakeEmbedder`) and a **real**
:class:`AsyncioEventBus`, no mocks (SDS §14.3). ``numpy`` is present here via the ``memory`` extra;
this suite — not line coverage, since adapters are omitted (P6) — is the proof the index and hybrid
retrieval behave. A few tests script the embedding vectors directly (a legitimate fake, not a mock)
where a precise cosine geometry is what is under test.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Sequence
from typing import NamedTuple
from uuid import UUID, uuid4

import pytest

from avid.adapters import (
    FakeEmbedder,
    FakeFactRepository,
    HybridRetriever,
    pack_embedding,
)
from avid.adapters.clock import FakeClock
from avid.core.event_bus import AsyncioEventBus
from avid.domain import Fact, MemoryRecallCompleted, ScoreWeights

_WEIGHTS = ScoreWeights()  # α=β=γ=1 — the §7.7 published baseline


def _fact(
    text: str,
    *,
    kind: str = "other",
    importance: int = 5,
    last_accessed: int = 1_000_000,
) -> Fact:
    return Fact(
        id=0,  # ignored on insert — the DB assigns the rowid
        text=text,
        kind=kind,  # type: ignore[arg-type]
        importance=importance,
        created_at=last_accessed,
        last_accessed_at=last_accessed,
    )


class Rig(NamedTuple):
    retriever: HybridRetriever
    repo: FakeFactRepository
    embedder: object  # FakeEmbedder or a scripted stand-in
    clock: FakeClock
    bus: AsyncioEventBus
    recalls: list[MemoryRecallCompleted]


async def _make_rig(
    *, embedder: object | None = None, top_k: int = 5, vector_pool: int = 50
) -> AsyncIterator[Rig]:
    clock = FakeClock()
    bus = AsyncioEventBus()
    repo = FakeFactRepository(clock=clock)
    emb = embedder if embedder is not None else FakeEmbedder()
    recalls: list[MemoryRecallCompleted] = []

    async def _record(event: MemoryRecallCompleted) -> None:
        recalls.append(event)

    bus.subscribe(MemoryRecallCompleted, _record, name="test.record_recall")
    await bus.start()
    retriever = HybridRetriever(
        repo=repo,
        embedder=emb,  # type: ignore[arg-type]
        bus=bus,
        clock=clock,
        top_k=top_k,
        half_life_days=14.0,
        weights=_WEIGHTS,
        vector_pool=vector_pool,
    )
    try:
        yield Rig(retriever, repo, emb, clock, bus, recalls)
    finally:
        await bus.stop()
        await repo.aclose()


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    async for r in _make_rig():
        yield r


async def _seed(rig: Rig, text: str, *, embed: bool = True, **kw: object) -> int:
    """Store a fact (through the real fake repo) with its FakeEmbedder vector, return its id."""
    vector = await rig.embedder.embed(text)  # type: ignore[attr-defined]
    blob = pack_embedding(vector) if embed else None
    return await rig.repo.add(_fact(text, **kw), embedding=blob)  # type: ignore[arg-type]


async def _wait_recalls(rig: Rig, n: int) -> None:
    for _ in range(200):
        if len(rig.recalls) >= n:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected {n} recall event(s), saw {len(rig.recalls)}")


# --- AC-3: one matmul over pre-normalised vectors, cosine is the dot product ------------------


async def test_query_equal_to_a_fact_ranks_it_first(rig: Rig) -> None:
    """A query identical to a fact's text embeds to the same unit vector → cosine 1.0 → top rank.

    No per-query normalisation pass: the pre-normalised vectors make the matmul the whole score."""
    ali = await _seed(rig, "the user's name is Ali", kind="identity")
    await _seed(rig, "the user drinks tea in the morning", kind="routine")
    await _seed(rig, "the user has a dog named Rex", kind="relationship")
    await rig.retriever.rebuild()

    result = await rig.retriever.retrieve("the user's name is Ali")
    assert result[0] == ali


async def test_retrieve_returns_at_most_top_k(rig: Rig) -> None:
    for i in range(9):
        await _seed(rig, f"the user likes hobby {i}", kind="preference")
    await rig.retriever.rebuild()
    result = await rig.retriever.retrieve("the user likes hobby")
    assert len(result) <= 5  # top_k default


# --- AC-4: hybrid ∪ — the keyword branch rescues a fact vector search would rank out ----------


class _ScriptedEmbedder:
    """An embedder whose vectors are dictated by the test (a fake, not a mock): it lets a case
    fix the exact cosine geometry the FakeEmbedder's bag-of-words would only approximate."""

    def __init__(self, table: dict[str, Sequence[float]], *, dimensions: int) -> None:
        self._table = table
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, text: str) -> Sequence[float]:
        return self._table[text]


async def test_keyword_branch_retrieves_a_fact_the_vector_pool_excluded() -> None:
    """§7.7's proper-noun case: with the vector pool holding only its single best match, a name
    query that vector search alone would miss is still retrieved — via the FTS5 ∪ branch."""
    q = "Maya"
    coffee = "the user likes coffee"
    sister = "Maya is my sister"
    table = {
        q: [1.0, 0.0, 0.0, 0.0],  # query vector
        coffee: [
            1.0,
            0.0,
            0.0,
            0.0,
        ],  # cosine 1.0 to the query — the lone vector winner
        sister: [0.0, 1.0, 0.0, 0.0],  # cosine 0.0 — vector search ranks it last
    }
    embedder = _ScriptedEmbedder(table, dimensions=4)
    # vector_pool=1: the vector side contributes only `coffee`; `sister` can reach the result set
    # solely because keyword_search("Maya") pulls it in. Remove the ∪ and `sister` is invisible.
    async for rig in _make_rig(embedder=embedder, vector_pool=1):
        coffee_id = await _seed(rig, coffee)
        sister_id = await _seed(rig, sister)
        await rig.retriever.rebuild()

        result = await rig.retriever.retrieve(q)
        assert sister_id in result  # rescued by the keyword branch
        assert coffee_id in result  # the union, not a replacement


# --- AC-2: SQLite is truth, the matrix is an index; the boot rebuild is the reconciliation ----


async def test_a_fact_inserted_behind_the_indexs_back_is_invisible_until_rebuild(
    rig: Rig,
) -> None:
    a = await _seed(rig, "the user enjoys coffee", kind="routine")
    await rig.retriever.rebuild()
    assert a in await rig.retriever.retrieve("the user enjoys coffee")

    # Mutate SQLite directly, bypassing append() — the matrix does not know about `b` yet.
    b = await _seed(rig, "the user enjoys coffee and tea", kind="routine")
    after = await rig.retriever.retrieve("the user enjoys coffee")
    assert (
        b not in after
    )  # the index gates visibility; the DB write alone is not enough

    await rig.retriever.rebuild()  # the reconciliation
    assert b in await rig.retriever.retrieve("the user enjoys coffee")


async def test_append_and_remove_keep_the_matrix_in_step_without_a_rebuild(
    rig: Rig,
) -> None:
    await _seed(rig, "the user enjoys coffee", kind="routine")
    await rig.retriever.rebuild()

    # A new fact reflected write-through (as #122's store_fact will): visible with no rebuild.
    text = "the user enjoys coffee and tea"
    fact = _fact(text, kind="routine")
    new_id = await rig.repo.add(
        fact, embedding=pack_embedding(await rig.embedder.embed(text))
    )  # type: ignore[attr-defined]
    rig.retriever.append(
        _fact_with_id(fact, new_id), pack_embedding(await rig.embedder.embed(text))
    )  # type: ignore[attr-defined]
    assert new_id in await rig.retriever.retrieve(text)

    rig.retriever.remove(new_id)
    assert new_id not in await rig.retriever.retrieve(text)


def _fact_with_id(fact: Fact, fid: int) -> Fact:
    return Fact(
        id=fid,
        text=fact.text,
        kind=fact.kind,
        importance=fact.importance,
        created_at=fact.created_at,
        last_accessed_at=fact.last_accessed_at,
    )


# --- AC-5: retrieval is live-only — superseded facts stay stored but never surface ------------


async def test_superseded_facts_never_surface(rig: Rig) -> None:
    old = await _seed(rig, "the user drinks coffee every morning", kind="routine")
    new = await _seed(rig, "the user switched to tea", kind="routine")
    await rig.repo.mark_superseded(old, new, at=1_500_000)
    await rig.retriever.rebuild()

    result = await rig.retriever.retrieve("the user drinks coffee every morning")
    assert (
        old not in result
    )  # history is retained in SQLite (§7.8) but excluded from recall


# --- similar(): the §7.8 supersession pre-check — cosine-threshold search, best-first, no event ---


async def test_similar_returns_only_facts_at_or_above_the_threshold_best_first() -> (
    None
):
    """``similar`` surfaces the ids whose cosine ≥ threshold, ordered by descending cosine. A scripted
    geometry pins the exact cosines against the query ``[1, 0, 0]`` so the cut is unambiguous."""
    q = "the user drinks coffee"
    same = "the user drinks coffee still"  # cosine 1.0
    near = "the user drinks coffee at 8am"  # cosine 0.9
    far = "the user has a cat"  # cosine 0.3
    table = {
        q: [1.0, 0.0, 0.0],
        same: [1.0, 0.0, 0.0],
        near: [0.9, math.sqrt(1 - 0.9**2), 0.0],
        far: [0.3, math.sqrt(1 - 0.3**2), 0.0],
    }
    embedder = _ScriptedEmbedder(table, dimensions=3)
    async for rig in _make_rig(embedder=embedder):
        same_id = await _seed(rig, same)
        near_id = await _seed(rig, near)
        far_id = await _seed(rig, far)
        await rig.retriever.rebuild()

        hits = await rig.retriever.similar(table[q], threshold=0.85, k=5)
        assert hits == (
            same_id,
            near_id,
        )  # best-first; far (0.3) is below the threshold
        assert far_id not in hits


async def test_similar_caps_at_k() -> None:
    """With more near-duplicates than ``k``, only the ``k`` best are returned."""
    table = {name: [1.0, 0.0] for name in ("q", "a", "b", "c")}
    embedder = _ScriptedEmbedder(table, dimensions=2)
    async for rig in _make_rig(embedder=embedder):
        for name in ("a", "b", "c"):
            await _seed(rig, name)
        await rig.retriever.rebuild()
        assert len(await rig.retriever.similar(table["q"], threshold=0.9, k=2)) == 2


async def test_similar_on_an_empty_index_returns_nothing(rig: Rig) -> None:
    await rig.retriever.rebuild()  # nothing seeded → no matrix
    assert await rig.retriever.similar([1.0, 0.0], threshold=0.5, k=5) == ()


async def test_similar_publishes_no_recall_event(rig: Rig) -> None:
    """Unlike :meth:`retrieve`, a near-duplicate search is not a recall — it must stay silent."""
    await _seed(rig, "the user's name is Ali", kind="identity")
    await rig.retriever.rebuild()
    vector = await rig.embedder.embed("the user's name is Ali")  # type: ignore[attr-defined]
    hits = await rig.retriever.similar(vector, threshold=0.5, k=5)
    assert hits  # the self-match clears the threshold
    await asyncio.sleep(0)  # give any stray publish a chance to land
    assert rig.recalls == []


# --- AC-7: publishes memory.recall_completed, latency from monotonic_ns ------------------------


async def test_retrieve_publishes_recall_completed(rig: Rig) -> None:
    await _seed(rig, "the user's name is Ali", kind="identity")
    await rig.retriever.rebuild()

    result = await rig.retriever.retrieve("what is the user's name")
    await _wait_recalls(rig, 1)

    event = rig.recalls[0]
    assert event.query == "what is the user's name"
    assert event.n_returned == len(result)
    # monotonic-derived: never negative (a wall-clock step could poison a timestamp_ms diff, §9.1.1).
    assert event.latency_ms >= 0.0


async def test_retrieve_propagates_the_turn_correlation_id(rig: Rig) -> None:
    await _seed(rig, "the user's name is Ali", kind="identity")
    await rig.retriever.rebuild()

    corr = uuid4()
    await rig.retriever.retrieve("name", correlation_id=corr)
    await _wait_recalls(rig, 1)
    assert rig.recalls[0].correlation_id == corr


async def test_retrieve_mints_a_correlation_id_when_standing_alone(rig: Rig) -> None:
    await _seed(rig, "the user's name is Ali", kind="identity")
    await rig.retriever.rebuild()
    await rig.retriever.retrieve("name")
    await _wait_recalls(rig, 1)
    assert isinstance(rig.recalls[0].correlation_id, UUID)


async def test_retrieve_on_an_empty_index_returns_nothing_but_still_reports(
    rig: Rig,
) -> None:
    await rig.retriever.rebuild()  # nothing seeded
    result = await rig.retriever.retrieve("anything at all")
    assert result == ()
    await _wait_recalls(rig, 1)
    assert rig.recalls[0].n_returned == 0


# --- AC-9: the scan stays cheap at the scale a single-user robot actually reaches --------------


class _CheapEmbedder:
    """A deterministic embedder with negligible per-call cost. AC-9 measures the *scan*, not the
    model, so the FakeEmbedder's per-token Gaussians (real, but heavy at thousands of facts) would
    only load the test loop, not the retriever. A one-hot in a modest dimension is stable within a
    run and gives distinct texts distinct vectors — all the scan test needs."""

    def __init__(self, *, dimensions: int = 64) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, text: str) -> Sequence[float]:
        vector = [0.0] * self._dimensions
        vector[hash(text) % self._dimensions] = 1.0
        return vector


async def test_scan_stays_cheap_at_a_few_thousand_facts() -> None:
    async for rig in _make_rig(embedder=_CheapEmbedder()):
        for i in range(2_000):
            await _seed(rig, f"item{i}")
            await asyncio.sleep(
                0
            )  # keep the bulk-seed loop's task steps short under the gate
        await rig.retriever.rebuild()

        start = time.perf_counter()
        result = await rig.retriever.retrieve("item1000")
        elapsed = time.perf_counter() - start

        assert (
            len(result) == 5
        )  # top_k — the matmul + rank returns a full page at scale
        # A loose ceiling: the real <50 ms P8 budget is enforced by the async-debug job's
        # slow-callback gate (the matmul runs inline); this only catches a gross O(N²)
        # regression without flaking on CI load.
        assert elapsed < 0.5


# --- #264: the FTS5 proper-noun branch must be able to change a result ------------------------


async def test_the_keyword_branch_can_change_a_result(rig: Rig) -> None:
    """#264 — removing FTS5 entirely must now change something. Before the fix it never did.

    This is the regression the issue is about, expressed the way the bug was found: run the real
    retriever, then neuter the keyword branch and diff. `_VECTOR_POOL` is 50, so at any store this
    robot realistically reaches every fact is *already* a vector candidate — which meant the
    keyword union added no ids, and ranking had no keyword term to consult. §7.7's proper-noun
    rescue was architecturally present and operationally dead.

    ``_CheapEmbedder`` is the point: it is one-hot on ``hash(text)``, so a query shares **no**
    vector similarity with any fact — the "proper nouns embed to mush" premise, made total. FTS5
    is then the only thing that can possibly distinguish the right fact, which is precisely the
    situation §7.7 built it for.
    """
    async for r in _make_rig(embedder=_CheapEmbedder()):
        # ⚠️ The target is seeded LAST, so it carries the highest fact_id. Every other component
        # is uniform across this store, so `rank_candidates` falls through to its ascending
        # fact_id tie-break and the target sorts DEAD LAST on everything except the keyword term.
        # Seeded first, it would rank first for a reason that has nothing to do with the fix —
        # the first draft of this test did exactly that and passed while proving nothing.
        for text in (
            "Ali takes his coffee black.",
            "Ali's sister is called Rana.",
            "Ali works as a mechanical engineer.",
        ):
            await _seed(r, text)
        target = await _seed(r, "Ali's neighbor's dog is called Biscuit.")
        await r.retriever.rebuild()

        with_fts = await r.retriever.retrieve("tell me about Biscuit")

        original = r.repo.keyword_search

        async def _no_keywords(query: str, *, limit: int) -> list[int]:
            return []

        r.repo.keyword_search = _no_keywords  # type: ignore[method-assign]
        try:
            without_fts = await r.retriever.retrieve("tell me about Biscuit")
        finally:
            r.repo.keyword_search = original  # type: ignore[method-assign]

        # The load-bearing assertion. It fails if δ is zeroed, if `keyword_hit` stops being
        # threaded through `_candidates`, or if the keyword branch is removed — i.e. it fails
        # for every way of reintroducing #264, and for no other reason.
        assert with_fts[0] == target  # FTS5's evidence lifts it from last to first
        assert without_fts[-1] == target  # ...and without that evidence it is last
        assert with_fts != without_fts
