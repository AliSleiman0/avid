"""Supersession/forget cosine calibration — the distribution behind two thresholds (#260, #257).

A **Tier-5** tool (SDS §14.7): scored and tracked, **never pass/fail**, so it **always exits 0**.
The number is the product. Invoked explicitly, never collected by ``pytest``, and it lives outside
``avid/`` so it is clear of mypy/coverage default scope — like ``tools/eval_recall.py``, whose shape
this follows.

**Why it exists.** Two bars over the same embedding space were both wrong, in opposite directions:

* ``[memory] supersession_threshold = 0.85`` admitted **nothing** — measured on the Pi, a close
  paraphrase of a fact scored 0.7917 against itself, so §7.8's judge was never called (#260).
* ``forget`` had **no bar at all** and deleted five unrelated facts on one call (#257).

Both were set without a distribution to look at. This produces the distribution.

**What it reports**, for each class in ``assets/eval/supersession.json``:

* the cosine spread (min / p50 / max) of ``contradictions``, ``paraphrases`` and ``unrelated``;
* a **threshold sweep** — at each candidate bar, how many pairs of each class it admits. That is the
  gate's recall against its cost, and it is the table the value should be argued from;
* the **overlap**: the worst contradiction against the best unrelated pair. If the former is below
  the latter there is *no* separating value, and saying so is the finding — not splitting the
  difference and calling it calibrated.

**The two error costs are not symmetric**, and the report says so rather than optimising a score:
admitting an unrelated pair costs one cheap off-turn-path TextModel call; rejecting a contradiction
leaves a wrong memory in place forever. For ``forget`` the asymmetry runs the other way — admitting
an unrelated fact **deletes user data irreversibly** (§7.10) — which is why one distribution feeds
two different bars.

⚠️ **Run it on the Pi with ``[adapters] embedder = "local_minilm"``.** ``FakeEmbedder`` is
bag-of-words; a value calibrated against it would be a number about the fake. The banner says so if
the config selects anything else.

Run::

    /opt/avid/.venv/bin/python tools/eval_supersession.py --config /etc/robot/config.toml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from avid.core.config import load_config
from avid.main import _build_embedder

_DEFAULT_PATH = (
    Path(__file__).resolve().parents[1] / "assets" / "eval" / "supersession.json"
)

# The classes, in the order they are reported. "should_admit" records what the §7.8 gate is trying
# to do with each — it is documentation for the reader of the table, not a pass/fail rule.
_CLASSES = (
    ("contradictions", "must reach the judge"),
    ("paraphrases", "should reach the judge"),
    ("unrelated", "must NOT reach the judge"),
)

_SWEEP = (0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90)


@dataclass(frozen=True, slots=True)
class Pair:
    id: str
    old: str
    new: str
    cls: str


def load_pairs(path: Path) -> tuple[Pair, ...]:
    """Parse the eval set. The ``_meta`` block is documentation and is skipped."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    pairs: list[Pair] = []
    for cls, _ in _CLASSES:
        for p in raw[cls]:
            pairs.append(Pair(id=p["id"], old=p["old"], new=p["new"], cls=cls))
    return tuple(pairs)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Dot product — both vectors are pre-normalised (§8.2), so this **is** the cosine.

    Deliberately the same arithmetic ``HybridRetriever.similar`` performs, rather than a
    re-implementation that normalises again: a calibration measured with different arithmetic than
    the gate uses would be a number about this file.
    """
    return sum(x * y for x, y in zip(a, b, strict=True))


def _p50(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _report(
    scored: list[tuple[Pair, float]], *, threshold: float, floor: float
) -> None:
    by_class: dict[str, list[float]] = {cls: [] for cls, _ in _CLASSES}
    for pair, c in scored:
        by_class[pair.cls].append(c)

    print("\n=== cosine spread by class ===")
    print(f"{'class':<16}{'n':>4}{'min':>9}{'p50':>9}{'max':>9}   intent")
    print("-" * 78)
    for cls, intent in _CLASSES:
        vals = by_class[cls]
        print(
            f"{cls:<16}{len(vals):>4}{min(vals):>9.4f}{_p50(vals):>9.4f}{max(vals):>9.4f}   {intent}"
        )

    print("\n=== threshold sweep — pairs admitted at each bar ===")
    print(
        f"{'bar':>6}{'contradictions':>17}{'paraphrases':>14}{'unrelated':>12}   note"
    )
    print("-" * 78)
    for bar in _SWEEP:
        adm = {cls: sum(1 for v in by_class[cls] if v >= bar) for cls, _ in _CLASSES}
        note = []
        if abs(bar - threshold) < 1e-9:
            note.append("<- supersession_threshold")
        if abs(bar - floor) < 1e-9:
            note.append("<- forget floor")
        print(
            f"{bar:>6.2f}"
            f"{adm['contradictions']:>10}/{len(by_class['contradictions']):<6}"
            f"{adm['paraphrases']:>8}/{len(by_class['paraphrases']):<5}"
            f"{adm['unrelated']:>7}/{len(by_class['unrelated']):<4}   {' '.join(note)}"
        )

    worst_contradiction = min(by_class["contradictions"])
    best_unrelated = max(by_class["unrelated"])
    print("\n=== separability ===")
    print(f"  worst contradiction : {worst_contradiction:.4f}")
    print(f"  best unrelated      : {best_unrelated:.4f}")
    if worst_contradiction > best_unrelated:
        print(
            f"  the classes SEPARATE — any bar in ({best_unrelated:.4f}, "
            f"{worst_contradiction:.4f}] admits every contradiction and no unrelated pair."
        )
    else:
        print(
            "  the classes OVERLAP — there is no value that admits every contradiction while\n"
            "  rejecting every unrelated pair. Choose from the sweep above by which error you can\n"
            "  afford, and record that reasoning; do not split the difference and call it calibrated."
        )

    print("\n=== the pairs a bar would get wrong, worst first ===")
    contradictions = sorted(
        ((p, c) for p, c in scored if p.cls == "contradictions"), key=lambda t: t[1]
    )
    unrelated = sorted(
        ((p, c) for p, c in scored if p.cls == "unrelated"),
        key=lambda t: t[1],
        reverse=True,
    )
    print("  hardest contradictions (rejected first as the bar rises):")
    for pair, c in contradictions[:5]:
        print(f"    {c:.4f}  {pair.id:<12} {pair.new[:56]}")
    print("  most confusable unrelated pairs (admitted first as the bar falls):")
    for pair, c in unrelated[:5]:
        print(f"    {c:.4f}  {pair.id:<12} {pair.new[:56]}")


async def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    pairs = load_pairs(args.path)
    embedder = _build_embedder(config)

    print(
        f"supersession calibration — {len(pairs)} pairs from {args.path.name}\n"
        f"embedder={config.adapters.embedder}  "
        f"supersession_threshold={config.memory.supersession_threshold}"
    )
    if config.adapters.embedder != "local_minilm":
        print(
            "\n⚠️  [adapters] embedder is NOT local_minilm. FakeEmbedder is bag-of-words: the\n"
            "    numbers below describe the FAKE's geometry and must not be used to choose a\n"
            "    threshold the real model will live under."
        )

    scored: list[tuple[Pair, float]] = []
    for pair in pairs:
        old_v = await embedder.embed(pair.old)
        new_v = await embedder.embed(pair.new)
        scored.append((pair, cosine(old_v, new_v)))

    floor = getattr(config.memory, "forget_relevance_floor", float("nan"))
    _report(scored, threshold=config.memory.supersession_threshold, floor=floor)

    if args.verbose:
        print("\n=== every pair ===")
        for pair, c in sorted(scored, key=lambda t: t[1], reverse=True):
            print(f"  {c:.4f}  {pair.cls:<15} {pair.id:<14} {pair.new[:50]}")

    print(
        "\nTier 5: this is a measurement, not a gate — it always exits 0 (SDS §14.7).\n"
        "Record the chosen bars and the reasoning in SDS §7.8; an unexplained threshold is how\n"
        "0.85 survived a whole milestone."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Supersession/forget cosine calibration (#260, #257) - Tier 5, always exits 0"
    )
    parser.add_argument(
        "--config", required=True, help="the TOML config (selects the embedder)"
    )
    parser.add_argument(
        "--path", type=Path, default=_DEFAULT_PATH, help="eval JSON file"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="also print every pair's cosine"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    asyncio.run(_run(args))
    return 0  # Tier 5: the number is the product, never a verdict


if __name__ == "__main__":
    raise SystemExit(main())
