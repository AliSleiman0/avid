"""Retrieval eval harness — recall@5 over the fact/query set (AVID-115, SDS §14.7).

A **Tier-5** tool (§14.7): scored and tracked over time, **never pass/fail**, because
retrieval quality is non-deterministic and *"a flaky red build teaches you to ignore red
builds."* So this **always exits 0** — the number is the product, not a gate. It is invoked
explicitly (`python tools/eval_recall.py`), never collected by the default `pytest` run, and
it lives outside `avid/` so it is clear of mypy/coverage default scope — like
`tools/generate_cue_bank.py`. CI lints the whole repo, so it stays ruff-clean and formatted.

It exists **before any retrieval code does** (deliberately — R-07, PMP §9.2): the eval set is
written first so its queries are not shaped by what an implementation already answers. Until
the real hybrid retriever lands (§7.7, #120) the harness scores a deliberately-bad **stub**,
which proves the measurement rig is wired end-to-end — the whole point of filing this first.

The scorer takes the retriever as a **narrow injected callable** (`Retriever`), not a class,
so the same code scores today's stub and, later, the real retriever with no change here.

Run:  ``python tools/eval_recall.py``  (optionally ``--path ...`` / ``--k 5``).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

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


def make_stub_retriever(evalset: EvalSet) -> Retriever:
    """A deliberately-bad retriever: returns the first k fact ids in file order, ignoring the
    query. It exists only to prove the harness is wired end-to-end (AC-4) — it will score a
    real, low number and never a good one."""
    ids = [f.id for f in evalset.facts]

    def _retrieve(query: str, k: int) -> Sequence[str]:
        return ids[:k]

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
    # Retrieval does not exist yet — score the stub so the rig is proven before #120.
    report = recall_at_k(evalset, make_stub_retriever(evalset), args.k)
    print(f"eval set: {len(evalset.facts)} facts, {len(evalset.queries)} queries")
    print(f"retriever: stub (first-{args.k}, query-blind)")
    print()
    print(format_report(report, args.k))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
