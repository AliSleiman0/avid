"""MemoryService — the sole writer/reader of persistent memory (#122, SDS §9.2, §9.1.4).

Driven end-to-end against the P6 fakes (:class:`FakeFactRepository`, :class:`FakeEmbedder`,
:class:`FakeTextModel`) and the **real** :class:`HybridRetriever` + :class:`AsyncioEventBus`, no mocks
(SDS §14.3). The service is application code (not a coverage-omitted adapter), so these tests are both
its behaviour proof *and* its line coverage. A couple of cases script the embedding vectors / the text
model directly (a legitimate fake, not a mock) where a precise cosine geometry or a forced supersession
decision is what is under test.

``numpy`` is present here via the ``memory`` extra; the whole suite runs clean under
``PYTHONASYNCIODEBUG=1`` — the store I/O rides the repo's writer thread, the matmul is inline sub-ms,
and the fake text model decides in-process.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from typing import NamedTuple
from uuid import uuid4

import pytest

from avid.adapters import (
    FakeEmbedder,
    FakeFactRepository,
    FakeTextModel,
    HybridRetriever,
)
from avid.adapters.clock import FakeClock
from avid.core.event_bus import AsyncioEventBus
from avid.domain import (
    Event,
    Fact,
    MemoryFactDeleted,
    MemoryFactStored,
    MemoryFactSuperseded,
    MemoryRecallCompleted,
    ScoreWeights,
    SystemHandlerFailed,
)
from avid.services import MemoryService

_RECORDED = (
    MemoryFactStored,
    MemoryFactSuperseded,
    MemoryFactDeleted,
    MemoryRecallCompleted,
    SystemHandlerFailed,
)


def _fact(text: str, *, kind: str = "other", importance: int = 5) -> Fact:
    # created_at / last_accessed_at left 0 so store_fact stamps them (exercising that branch).
    return Fact(
        id=0,
        text=text,
        kind=kind,  # type: ignore[arg-type]
        importance=importance,
        created_at=0,
        last_accessed_at=0,
    )


class _ScriptedEmbedder:
    """An embedder whose vectors the test dictates — a fake, not a mock — so a case can fix the exact
    cosine a supersession pre-check turns on."""

    def __init__(self, table: dict[str, Sequence[float]], *, dimensions: int) -> None:
        self._table = table
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, text: str) -> Sequence[float]:
        return self._table[text]


class _CheapEmbedder:
    """A deterministic embedder with negligible per-call cost, for cases that store *many* facts. The
    FakeEmbedder's per-token Gaussians are real but heavy in a tight loop and would load the test loop,
    not the code under test (P8) — a one-hot in a modest dimension gives distinct texts distinct vectors,
    all a count/budget test needs (mirrors ``test_retrieval``'s scan embedder)."""

    def __init__(self, *, dimensions: int = 64) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, text: str) -> Sequence[float]:
        vector = [0.0] * self._dimensions
        vector[hash(text) % self._dimensions] = 1.0
        return vector


class _AlwaysSupersede:
    """A text model that judges every candidate superseded — forces the §7.8 write path deterministically,
    independent of the fake's lexical rule."""

    async def judge_supersession(
        self, *, new_fact: str, candidates: Sequence[tuple[int, str]]
    ) -> Sequence[int]:
        return [cid for cid, _ in candidates]


class _BoomTextModel:
    """A text model that always raises — the real ``openai`` adapter's failure modes (timeout, API error,
    an unparseable reply) all surface at this seam as an exception. AC-9: a judge failure inside a tool
    handler must degrade to no supersession, never lose the write or crash (the ``FakeTextModel`` never
    raises, so this stand-in is the only thing that exercises the guard)."""

    async def judge_supersession(
        self, *, new_fact: str, candidates: Sequence[tuple[int, str]]
    ) -> Sequence[int]:
        raise RuntimeError("text model timed out")


class Rig(NamedTuple):
    memory: MemoryService
    repo: FakeFactRepository
    retriever: HybridRetriever
    embedder: object
    clock: FakeClock
    bus: AsyncioEventBus
    events: list[Event]


async def _make_rig(
    *,
    embedder: object | None = None,
    text_model: object | None = None,
    boom_on: type[Event] | None = None,
    supersession_threshold: float = 0.60,
    forget_relevance_floor: float = 0.65,
    forget_k: int = 5,
) -> AsyncIterator[Rig]:
    clock = FakeClock()
    bus = AsyncioEventBus()
    repo = FakeFactRepository(clock=clock)
    emb = embedder if embedder is not None else FakeEmbedder()
    tm = text_model if text_model is not None else FakeTextModel()
    events: list[Event] = []

    async def _record(event: Event) -> None:
        events.append(event)

    for event_type in _RECORDED:
        bus.subscribe(event_type, _record, name=f"test.record.{event_type.__name__}")
    if boom_on is not None:

        async def _boom(event: Event) -> None:
            raise RuntimeError("subscriber blew up")

        bus.subscribe(boom_on, _boom, name="test.boom")

    await bus.start()
    retriever = HybridRetriever(
        repo=repo,
        embedder=emb,  # type: ignore[arg-type]
        bus=bus,
        clock=clock,
        top_k=5,
        half_life_days=14.0,
        weights=ScoreWeights(),
    )
    memory = MemoryService(
        bus=bus,
        clock=clock,
        repo=repo,
        retriever=retriever,
        embedder=emb,  # type: ignore[arg-type]
        text_model=tm,  # type: ignore[arg-type]
        supersession_threshold=supersession_threshold,
        supersession_k=5,
        forget_relevance_floor=forget_relevance_floor,
        forget_k=forget_k,
        top_facts_max=15,
        top_facts_token_budget=600,
    )
    try:
        yield Rig(memory, repo, retriever, emb, clock, bus, events)
    finally:
        await memory.stop()  # closes the store
        await bus.stop()


async def _drain(rig: Rig, *, ticks: int = 30) -> None:
    """Let the bus workers deliver everything queued so far (publish is fire-and-forget)."""
    for _ in range(ticks):
        await asyncio.sleep(0)


async def _wait(rig: Rig, pred: Callable[[], bool], *, tries: int = 300) -> None:
    for _ in range(tries):
        if pred():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


def _of(rig: Rig, event_type: type[Event]) -> list[Event]:
    return [e for e in rig.events if isinstance(e, event_type)]


# --- AC-6 boot + AC-2 store/retrieve round-trip, durable-before-return ------------------------


async def test_start_rebuilds_and_store_then_retrieve_roundtrips() -> None:
    async for rig in _make_rig():
        await rig.memory.start()  # boot rebuild on an empty store
        fid = await rig.memory.store_fact(
            _fact("the user's name is Ali", kind="identity")
        )
        got = await rig.memory.retrieve("the user's name is Ali")
        assert any(isinstance(f, Fact) and f.id == fid for f in got)


async def test_store_fact_is_durable_before_it_returns() -> None:
    async for rig in _make_rig():
        await rig.memory.start()
        fid = await rig.memory.store_fact(
            _fact("the user likes tea", kind="preference")
        )
        # the row is in the store the instant store_fact returns — before any event is drained
        assert await rig.repo.get(fid) is not None


async def test_store_fact_publishes_fact_stored_after_the_write() -> None:
    async for rig in _make_rig():
        await rig.memory.start()
        fid = await rig.memory.store_fact(
            _fact("the user's name is Ali", kind="identity", importance=7)
        )
        await _drain(rig)
        stored = _of(rig, MemoryFactStored)
        assert len(stored) == 1
        event = stored[0]
        assert isinstance(event, MemoryFactStored)
        assert event.fact_id == fid
        assert event.kind == "identity"
        assert event.importance == 7


async def test_store_fact_stamps_the_turn_correlation_id() -> None:
    async for rig in _make_rig():
        await rig.memory.start()
        corr = uuid4()
        fid = await rig.memory.store_fact(
            _fact("the user likes tea", kind="preference"), correlation_id=corr
        )
        stored = await rig.repo.get(fid)
        assert stored is not None and stored.source_correlation_id == corr
        await _drain(rig)
        assert _of(rig, MemoryFactStored)[0].correlation_id == corr


# --- §7.8 supersession-on-write ----------------------------------------------------------------


async def test_store_fact_supersedes_a_contradicting_fact() -> None:
    """Scripted so the new fact's vector matches the old one's (cosine 1.0 ≥ 0.85 → a candidate), and a
    forced text-model decision supersedes it: the old fact is pointed at the new one, dropped from live
    retrieval, and ``memory.fact_superseded`` is published."""
    old_text = "the user drinks coffee"
    new_text = "the user switched to tea"
    table = {old_text: [1.0, 0.0], new_text: [1.0, 0.0]}
    embedder = _ScriptedEmbedder(table, dimensions=2)
    async for rig in _make_rig(embedder=embedder, text_model=_AlwaysSupersede()):
        await rig.memory.start()
        old_id = await rig.memory.store_fact(_fact(old_text, kind="routine"))
        new_id = await rig.memory.store_fact(_fact(new_text, kind="routine"))

        old = await rig.repo.get(old_id)
        assert (
            old is not None and old.superseded_by == new_id
        )  # soft supersession in SQLite
        live_ids = [f.id for f in await rig.memory.retrieve(old_text)]
        assert (
            old_id not in live_ids and new_id in live_ids
        )  # old gone from recall, new present

        await _drain(rig)
        superseded = _of(rig, MemoryFactSuperseded)
        assert len(superseded) == 1
        event = superseded[0]
        assert isinstance(event, MemoryFactSuperseded)
        assert event.old_id == old_id and event.new_id == new_id


async def test_a_failing_text_model_degrades_to_storing_without_supersession(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC-9: the new fact clears the cosine pre-check (so the judge *is* consulted), but the judge raises.
    The write must still commit — the fact is stored and its id returned — nothing is superseded (history
    is not rewritten this time), and the failure is logged with the turn's correlation id."""
    old_text = "the user drinks coffee"
    new_text = "the user switched to tea"
    table = {
        old_text: [1.0, 0.0],
        new_text: [1.0, 0.0],
    }  # cosine 1.0 ≥ 0.85 → a candidate
    embedder = _ScriptedEmbedder(table, dimensions=2)
    async for rig in _make_rig(embedder=embedder, text_model=_BoomTextModel()):
        await rig.memory.start()
        old_id = await rig.memory.store_fact(_fact(old_text, kind="routine"))
        corr = uuid4()
        with caplog.at_level(logging.WARNING):
            new_id = await rig.memory.store_fact(
                _fact(new_text, kind="routine"), correlation_id=corr
            )

        assert (
            await rig.repo.get(new_id) is not None
        )  # the write survived the judge failure
        old = await rig.repo.get(old_id)
        assert old is not None and old.superseded_by is None  # nothing was superseded
        assert "supersession judge failed" in caplog.text
        assert str(corr) in caplog.text  # logged with the turn's correlation id (AC-9)

        await _drain(rig)
        assert _of(rig, MemoryFactSuperseded) == []
        assert len(_of(rig, MemoryFactStored)) == 2  # both facts stored, both notified


async def test_store_fact_without_contradiction_publishes_only_fact_stored() -> None:
    async for (
        rig
    ) in _make_rig():  # real FakeEmbedder + FakeTextModel (declines low-overlap facts)
        await rig.memory.start()
        await rig.memory.store_fact(_fact("the user drinks coffee", kind="routine"))
        await rig.memory.store_fact(
            _fact("the user has a sister named Maya", kind="relationship")
        )
        await _drain(rig)
        assert _of(rig, MemoryFactSuperseded) == []
        assert len(_of(rig, MemoryFactStored)) == 2


# --- AC-5 forget: hard cascading delete, SQLite then matrix ------------------------------------


async def test_forget_hard_deletes_matching_facts() -> None:
    async for rig in _make_rig():
        await rig.memory.start()
        fid = await rig.memory.store_fact(
            _fact("the user's sister is Maya", kind="relationship")
        )
        deleted = await rig.memory.forget("Maya")

        # Exactly one, not ">= 1". The loose form stayed green while forget was deleting the whole
        # top-k — it is the assertion that let #257 through, so it is the one that changed.
        assert deleted == 1
        assert await rig.repo.get(fid) is None  # row + FTS shadow + cascade gone
        assert [
            f.id for f in await rig.memory.retrieve("Maya")
        ] == []  # cannot resurface
        await _drain(rig)
        assert any(
            isinstance(e, MemoryFactDeleted) and e.fact_id == fid for e in rig.events
        )


async def test_forget_with_no_match_returns_zero() -> None:
    async for rig in _make_rig():
        await rig.memory.start()
        assert await rig.memory.forget("nothing has been stored") == 0


# --- AC-3 / AC-4 / no-double-publish -----------------------------------------------------------


async def test_subscriptions_is_empty() -> None:
    """§3.6.1: MemoryService is reached by direct call, never over the bus."""
    async for rig in _make_rig():
        assert rig.memory.subscriptions() == ()


async def test_retrieve_publishes_exactly_one_recall_completed() -> None:
    """The retriever owns the recall event; MemoryService must not double-publish it."""
    async for rig in _make_rig():
        await rig.memory.start()
        await rig.memory.store_fact(_fact("the user's name is Ali", kind="identity"))
        await rig.memory.retrieve("name")
        await _drain(rig)
        assert len(_of(rig, MemoryRecallCompleted)) == 1


async def test_top_facts_respects_count_and_token_budget() -> None:
    # A cheap embedder: this case stores many facts, and the FakeEmbedder's per-token compute in a
    # tight loop would load the test loop rather than the code under test (P8), like #120's scan test.
    async for rig in _make_rig(embedder=_CheapEmbedder()):
        await rig.memory.start()
        await rig.memory.store_fact(_fact("the user's name is Ali", kind="identity"))
        for i in range(20):
            await rig.memory.store_fact(_fact(f"note number {i}", importance=5))
            await asyncio.sleep(
                0
            )  # keep the bulk-store loop's task steps short under the gate
        top = await rig.memory.top_facts()
        assert len(top) <= 15  # bounded by count (~10–15)
        assert any(f.kind == "identity" for f in top)  # identity is always injected


# --- AC-8 a raising subscriber never undoes a committed write ----------------------------------


async def test_a_raising_fact_stored_subscriber_does_not_undo_the_write() -> None:
    async for rig in _make_rig(boom_on=MemoryFactStored):
        await rig.memory.start()
        fid = await rig.memory.store_fact(
            _fact("the user likes jazz", kind="preference")
        )
        assert (
            await rig.repo.get(fid) is not None
        )  # committed regardless of the subscriber
        # the bus swallowed the raise and republished system.handler_failed (§3.5.2)
        await _wait(rig, lambda: bool(_of(rig, SystemHandlerFailed)))


# --- #125 the MemoryTools tool surface (remember_fact / recall) --------------------------------


async def test_remember_fact_builds_and_stores_the_fact() -> None:
    # remember_fact(text, kind, importance) is the turn→Fact construction behind the port (§7.6): it
    # builds the Fact the tool arguments imply and defers to store_fact, durable before it returns.
    async for rig in _make_rig():
        await rig.memory.start()
        corr = uuid4()
        fid = await rig.memory.remember_fact(
            text="the user's dog is called biscuit",
            kind="relationship",
            importance=6,
            correlation_id=corr,
        )
        stored = await rig.repo.get(fid)  # durable the instant it returns (AC-3)
        assert stored is not None
        assert stored.text == "the user's dog is called biscuit"
        assert stored.kind == "relationship"
        assert stored.importance == 6
        assert stored.source_correlation_id == corr  # traces to the turn (§3.12.2)
        await _drain(rig)
        assert [e.fact_id for e in _of(rig, MemoryFactStored)] == [fid]


async def test_remember_fact_rejects_an_unknown_kind() -> None:
    # AC-2: a kind outside FACT_KINDS is refused before any write, so a bad tool argument becomes a
    # tool error — never a row the §8.3 CHECK rejects only at COMMIT.
    async for rig in _make_rig():
        await rig.memory.start()
        with pytest.raises(ValueError, match="unknown fact kind"):
            await rig.memory.remember_fact(text="x", kind="mood", importance=5)


@pytest.mark.parametrize("importance", [0, 11, -1])
async def test_remember_fact_rejects_out_of_range_importance(importance: int) -> None:
    async for rig in _make_rig():
        await rig.memory.start()
        with pytest.raises(ValueError, match="importance"):
            await rig.memory.remember_fact(
                text="x", kind="other", importance=importance
            )


async def test_recall_returns_facts_capped_at_k() -> None:
    # recall delegates to retrieve (which publishes memory.recall_completed) and truncates to the
    # model-requested k, so a model asking for fewer than top_k gets exactly that many.
    async for rig in _make_rig(embedder=_CheapEmbedder()):
        await rig.memory.start()
        for i in range(5):
            await rig.memory.remember_fact(
                text=f"fact number {i}", kind="other", importance=5
            )
            await asyncio.sleep(0)
        got = await rig.memory.recall("fact number", k=2)
        assert len(got) <= 2
        assert all(isinstance(f, Fact) for f in got)
        await _drain(rig)
        assert len(_of(rig, MemoryRecallCompleted)) == 1  # the retriever published it


# --- #257: forget deletes only what it can justify ---------------------------------------------


async def test_forget_deletes_one_fact_not_the_whole_top_k() -> None:
    """The M7 gate scenario, reduced to a test (#257 AC-2).

    On the Pi a single `forget` destroyed **five of six facts**: it deleted every id the top-k
    returned, so the store's whole population came back as "matches" and was hard-deleted, cascade
    and all. Here six facts exist, one query names one of them, and exactly one row goes.

    The vectors are scripted rather than left to ``FakeEmbedder``, because the precise cosine is the
    thing under test — a legitimate fake, not a mock (§14.3).
    """
    target = "the user drinks black coffee with no sugar"
    # Three unrelated facts, not the gate's five: the claim is "forget does not sweep the whole
    # candidate set", which four rows prove as well as six. Six put this test 3 ms over the P8
    # slow-callback bar, and eating another test's margin to make a point twice is a bad trade.
    others = [
        "the user lives in Beirut",
        "the user works as an engineer",
        "the user runs on Tuesday mornings",
    ]
    query = "coffee"
    # The target sits at cosine 1.0 with the query; the others at 0.30 — the 0.2-0.4 band the real
    # store showed for facts nobody would call a match.
    table: dict[str, Sequence[float]] = {target: [1.0, 0.0], query: [1.0, 0.0]}
    for text in others:
        table[text] = [0.30, (1 - 0.30**2) ** 0.5]

    embedder = _ScriptedEmbedder(table, dimensions=2)
    async for rig in _make_rig(embedder=embedder, forget_relevance_floor=0.60):
        await rig.memory.start()
        target_id = await rig.memory.store_fact(_fact(target, kind="preference"))
        other_ids = [
            await rig.memory.store_fact(_fact(t, kind="other")) for t in others
        ]

        deleted = await rig.memory.forget(query)

        assert deleted == 1, "forget must not sweep the top-k"
        assert await rig.repo.get(target_id) is None
        for fid in other_ids:
            assert await rig.repo.get(fid) is not None, (
                "an unrelated fact was destroyed — the #257 defect. The DELETE cascades, so "
                "there is nothing to undo it with"
            )


async def test_forget_with_a_query_that_matches_nothing_deletes_nothing() -> None:
    """#257 AC-3. §7.7 records that a negative query still returns its best-but-irrelevant
    matches — for a recall that costs a wasted line of context; for a forget it cost five facts."""
    stored = "the user lives in Beirut"
    query = "something entirely unrelated"
    table: dict[str, Sequence[float]] = {
        stored: [1.0, 0.0],
        query: [0.15, (1 - 0.15**2) ** 0.5],
    }
    embedder = _ScriptedEmbedder(table, dimensions=2)
    async for rig in _make_rig(embedder=embedder, forget_relevance_floor=0.60):
        await rig.memory.start()
        fid = await rig.memory.store_fact(_fact(stored, kind="identity"))

        assert await rig.memory.forget(query) == 0
        assert await rig.repo.get(fid) is not None


async def test_forget_still_works_by_proper_noun_below_the_floor() -> None:
    """The disjunction earning its keep: the FTS5 branch (§7.7 proper nouns).

    "Biscuit" embeds to something generic — scripted here at 0.10, far under the floor — but FTS5
    matches the term exactly. A floor-only policy would leave the fact in place while telling the
    user it was forgotten, which is a privacy failure wearing a success message.
    """
    stored = "the neighbour dog is called Biscuit"
    query = "Biscuit"
    table: dict[str, Sequence[float]] = {
        stored: [1.0, 0.0],
        query: [0.10, (1 - 0.10**2) ** 0.5],
    }
    embedder = _ScriptedEmbedder(table, dimensions=2)
    async for rig in _make_rig(embedder=embedder, forget_relevance_floor=0.60):
        await rig.memory.start()
        fid = await rig.memory.store_fact(_fact(stored, kind="relationship"))

        assert await rig.memory.forget(query) == 1
        assert await rig.repo.get(fid) is None


async def test_forget_reports_exactly_what_it_deleted() -> None:
    """The count returned, the rows gone and the events published must agree — a bounded forget
    that under-reports would be its own privacy bug."""
    a, b = "the user drinks coffee", "the user enjoys espresso"
    table: dict[str, Sequence[float]] = {
        a: [1.0, 0.0],
        b: [1.0, 0.0],
        "coffee": [1.0, 0.0],
    }
    embedder = _ScriptedEmbedder(table, dimensions=2)
    async for rig in _make_rig(embedder=embedder, forget_relevance_floor=0.60):
        await rig.memory.start()
        ids = {
            await rig.memory.store_fact(_fact(a, kind="preference")),
            await rig.memory.store_fact(_fact(b, kind="preference")),
        }
        deleted = await rig.memory.forget("coffee")
        await _drain(rig)

        assert deleted == len(ids)
        events = _of(rig, MemoryFactDeleted)
        assert {e.fact_id for e in events} == ids  # type: ignore[union-attr]


# --- #260: the gate must admit what its judge exists to decide ---------------------------------


class _CountingTextModel:
    """Wraps a judge and counts calls — a fake that records, not a mock that asserts.

    The point of #260 is that ``judge_supersession`` was **never invoked**: the cosine gate at 0.85
    rejected every candidate, so a test checking only the *outcome* cannot tell "the judge said no"
    from "the judge was never asked". This tells them apart.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.calls = 0

    async def judge_supersession(
        self, *, new_fact: str, candidates: Sequence[tuple[int, str]]
    ) -> Sequence[int]:
        self.calls += 1
        return await self._inner.judge_supersession(  # type: ignore[attr-defined]
            new_fact=new_fact, candidates=candidates
        )


async def test_a_pair_above_the_gate_reaches_the_judge() -> None:
    """The mechanism, not the value: with the bar injected below the pair cosine, the judge is
    consulted. The *value* is chosen from ``assets/eval/supersession.json`` against real
    embeddings — Tier 5, never a CI gate (§14.7), because CI runs the bag-of-words fake."""
    old_text, new_text = "the user drinks coffee", "the user switched to tea"
    table: dict[str, Sequence[float]] = {
        old_text: [1.0, 0.0],
        new_text: [0.70, (1 - 0.70**2) ** 0.5],
    }
    judge = _CountingTextModel(_AlwaysSupersede())
    async for rig in _make_rig(
        embedder=_ScriptedEmbedder(table, dimensions=2),
        text_model=judge,
        supersession_threshold=0.60,
    ):
        await rig.memory.start()
        old_id = await rig.memory.store_fact(_fact(old_text, kind="preference"))
        await rig.memory.store_fact(_fact(new_text, kind="preference"))

        assert judge.calls == 1, "the cosine gate must hand this pair to the judge"
        old = await rig.repo.get(old_id)
        assert old is not None and old.superseded_by is not None


async def test_a_pair_below_the_gate_never_reaches_the_judge() -> None:
    """The gate still has a cost job: an unrelated write must not buy a TextModel call. This is the
    half that keeps the #260 fix from becoming "ask the judge about everything"."""
    old_text, new_text = "the user drinks coffee", "the user lives in Beirut"
    table: dict[str, Sequence[float]] = {
        old_text: [1.0, 0.0],
        new_text: [0.20, (1 - 0.20**2) ** 0.5],
    }
    judge = _CountingTextModel(_AlwaysSupersede())
    async for rig in _make_rig(
        embedder=_ScriptedEmbedder(table, dimensions=2),
        text_model=judge,
        supersession_threshold=0.60,
    ):
        await rig.memory.start()
        await rig.memory.store_fact(_fact(old_text, kind="preference"))
        await rig.memory.store_fact(_fact(new_text, kind="identity"))

        assert judge.calls == 0


async def test_forgetting_a_fact_that_superseded_another_does_not_raise() -> None:
    """Deleting the newer half of a supersession pair must work, and must free the older half.

    The §8.3 schema declares ``superseded_by ... ON DELETE SET NULL`` next to
    ``CHECK ((superseded_by IS NULL) = (superseded_at IS NULL))``. Those disagree: the cascade nulls
    the pointer, leaves the timestamp, and SQLite rejects its own write. Reached by an ordinary
    sequence — state a fact, contradict it, ask to forget the newer one — and it raised
    ``IntegrityError`` **inside a tool handler**, abandoning the write. Found by the #257 tests.

    The older fact returns to live retrieval, which is what SET NULL was chosen to mean: nothing
    supersedes it any more. Cascading instead would destroy a fact the user never asked to forget.
    """
    old_text, new_text = "the user drinks coffee", "the user drinks tea"
    table: dict[str, Sequence[float]] = {
        old_text: [1.0, 0.0],
        new_text: [1.0, 0.0],
        "tea": [1.0, 0.0],
    }
    async for rig in _make_rig(
        embedder=_ScriptedEmbedder(table, dimensions=2),
        text_model=_AlwaysSupersede(),
        forget_relevance_floor=0.60,
    ):
        await rig.memory.start()
        old_id = await rig.memory.store_fact(_fact(old_text, kind="preference"))
        new_id = await rig.memory.store_fact(_fact(new_text, kind="preference"))

        superseded = await rig.repo.get(old_id)
        assert superseded is not None and superseded.superseded_by == new_id

        assert await rig.memory.forget("tea") == 1  # must not raise

        assert await rig.repo.get(new_id) is None
        freed = await rig.repo.get(old_id)
        assert freed is not None, (
            "forgetting the newer fact must not destroy the older one"
        )
        assert freed.superseded_by is None and freed.superseded_at is None
