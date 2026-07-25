"""Retrieval eval harness — recall@5 over the fact/query set (AVID-115, SDS §14.7).

A **Tier-5** tool (§14.7): scored and tracked over time, **never pass/fail**, because
retrieval quality is non-deterministic and *"a flaky red build teaches you to ignore red
builds."* So this **always exits 0** — the number is the product, not a gate. It is invoked
explicitly (`python tools/eval_recall.py`), never collected by the default `pytest` run, and
it lives outside `avid/` so it is clear of mypy/coverage default scope — like
`tools/generate_cue_bank.py`. CI lints the whole repo, so it stays ruff-clean and formatted.

It was written **before any retrieval code existed** (deliberately — R-07, PMP §9.2): the eval
set came first so its queries were not shaped by what an implementation already answers. As of
#120 it scores the **real** hybrid retriever (§7.7 — FTS5 ∪ cosine over the §8.5 index, the
`FakeEmbedder` standing in for the ONNX model), and the recall@5 it prints is the R-07 tripwire.

The scorer takes the retriever as a **narrow injected callable** (`Retriever`), not a class, so
the same code scored yesterday's stub and today's real retriever with no change to the scorer.

Run:  ``python tools/eval_recall.py``  (optionally ``--path ...`` / ``--k 5``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from avid.adapters import (
    FakeEmbedder,
    FakeFactRepository,
    HybridRetriever,
    pack_embedding,
)
from avid.adapters.clock import FakeClock
from avid.core.event_bus import AsyncioEventBus
from avid.domain import Fact, ScoreWeights

# The injected callable the scorer measures (AC-3): (query, k) -> ordered fact ids, most
# relevant first. May return FEWER than k, **including empty** — an empty result is how a real
# hybrid retriever with a relevance floor answers a query that has no matching fact (the
# negative probes). Retrieval does not exist yet (#120); a stub stands in.
Retriever = Callable[[str, int], Sequence[str]]

_DEFAULT_PATH = (
    Path(__file__).resolve().parents[1] / "assets" / "eval" / "retrieval.json"
)
_DEFAULT_K = 5


@dataclass(frozen=True, slots=True)
class EvalFact:
    id: str
    text: str


@dataclass(frozen=True, slots=True)
class EvalQuery:
    query: str
    expect: str | None  # a fact id, or None for a negative probe (no fact should match)
    category: str


@dataclass(frozen=True, slots=True)
class EvalSet:
    facts: tuple[EvalFact, ...]
    queries: tuple[EvalQuery, ...]


@dataclass(frozen=True, slots=True)
class Report:
    hits: int
    total: int
    per_category: dict[str, tuple[int, int]]  # category -> (hits, total)

    @property
    def recall(self) -> float:
        return self.hits / self.total if self.total else 0.0


def load_eval_set(path: Path) -> EvalSet:
    """Parse the JSON eval set into typed values. The ``_meta`` block is documentation."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    facts = tuple(EvalFact(id=f["id"], text=f["text"]) for f in raw["facts"])
    queries = tuple(
        EvalQuery(query=q["query"], expect=q["expect"], category=q["category"])
        for q in raw["queries"]
    )
    return EvalSet(facts=facts, queries=queries)


def _is_hit(expect: str | None, returned: Sequence[str], k: int) -> bool:
    """A positive query hits if its fact is in the top-k; a negative hits if the retriever
    correctly surfaced nothing (an empty result)."""
    if expect is None:
        return len(returned) == 0
    return expect in returned[:k]


def recall_at_k(evalset: EvalSet, retriever: Retriever, k: int = _DEFAULT_K) -> Report:
    """Score the retriever over the set: overall recall@k plus a per-category breakdown.

    Pure — the retriever is injected, so the same harness scores today's stub and, later,
    the real hybrid retriever (#120) unchanged.
    """
    hits = 0
    cat_hits: Counter[str] = Counter()
    cat_total: Counter[str] = Counter()
    for q in evalset.queries:
        cat_total[q.category] += 1
        if _is_hit(q.expect, retriever(q.query, k), k):
            hits += 1
            cat_hits[q.category] += 1
    per_category = {cat: (cat_hits[cat], cat_total[cat]) for cat in sorted(cat_total)}
    return Report(hits=hits, total=len(evalset.queries), per_category=per_category)


async def _compute_results(evalset: EvalSet, k: int) -> dict[str, tuple[str, ...]]:
    """Build the real hybrid retriever over the eval facts and run every query through it once.

    The eval set keys facts by string id; the store assigns integer rowids, so a small map carries
    the retriever's int ids back to the eval ids the scorer compares. Facts are embedded with the
    ``FakeEmbedder`` (the CI/sim embedder, P6) and stored with their §8.2 BLOB, then the index is
    rebuilt from the store exactly as it is at boot (§8.5)."""
    clock = FakeClock()
    bus = AsyncioEventBus(clock=clock)
    repo = FakeFactRepository(clock=clock)
    embedder = FakeEmbedder()
    retriever = HybridRetriever(
        repo=repo,
        embedder=embedder,
        bus=bus,
        clock=clock,
        top_k=k,
        half_life_days=14.0,
        weights=ScoreWeights(),
    )
    await bus.start()
    try:
        eval_id: dict[int, str] = {}
        now = clock.now()
        for fact in evalset.facts:
            vector = await embedder.embed(fact.text)
            stored = Fact(
                id=0,
                text=fact.text,
                kind="other",
                importance=5,
                created_at=now,
                last_accessed_at=now,
            )
            rowid = await repo.add(stored, embedding=pack_embedding(vector))
            eval_id[rowid] = fact.id
        await retriever.rebuild()
        results: dict[str, tuple[str, ...]] = {}
        for query in evalset.queries:
            ids = await retriever.retrieve(query.query)
            results[query.query] = tuple(eval_id[i] for i in ids)
        return results
    finally:
        await bus.stop()
        await repo.aclose()


def make_real_retriever(evalset: EvalSet, k: int) -> Retriever:
    """The real §7.7 hybrid retriever as the scorer's injected callable (AC-8).

    Retrieval is async and its store/index are built once, so results are computed up front in one
    event loop; the returned callable is a pure lookup, keeping :func:`recall_at_k` synchronous and
    retriever-agnostic."""
    results = asyncio.run(_compute_results(evalset, k))

    def _retrieve(query: str, _k: int) -> Sequence[str]:
        return results.get(query, ())

    return _retrieve


def format_report(report: Report, k: int) -> str:
    """Render overall recall@k and the per-category table for the console."""
    lines = [
        f"recall@{k} = {report.recall:.2f} ({report.hits}/{report.total})",
        "",
        f"{'category':<14}recall",
    ]
    for cat, (hits, total) in report.per_category.items():
        rate = hits / total if total else 0.0
        cell = f"{rate:.2f} ({hits}/{total})"
        lines.append(f"{cat:<14}{cell}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eval_recall",
        description=(
            "Score a retriever as recall@k over the M7 eval set (SDS §14.7, R-07). "
            "Tier 5: tracked over time, never pass/fail — always exits 0."
        ),
    )
    parser.add_argument(
        "--path", type=Path, default=_DEFAULT_PATH, help="eval JSON file"
    )
    parser.add_argument(
        "--k", type=int, default=_DEFAULT_K, help="cutoff k (default 5)"
    )
    args = parser.parse_args(argv)

    evalset = load_eval_set(args.path)
    report = recall_at_k(evalset, make_real_retriever(evalset, args.k), args.k)
    print(f"eval set: {len(evalset.facts)} facts, {len(evalset.queries)} queries")
    print(f"retriever: hybrid FTS5 + cosine, FakeEmbedder (top-{args.k}, #120)")
    print()
    print(format_report(report, args.k))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
