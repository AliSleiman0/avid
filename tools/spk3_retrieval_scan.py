"""SPK-3 (#127) — what the §7.7 retrieval scan actually costs on this device.

ADR-005 chose numpy brute-force cosine over pre-normalised 384-dim vectors, and §7.7 publishes a
scale table that is **extrapolated, not measured**. This converts that guess into a number, on the
hardware that has to live with it. PMP §6.4 time-box: 0.5 IED — **report the number and stop**.

**§7.7 budgets two different things, and conflating them is how a spike measures the wrong one:**

* **< 50 ms — the scan.** The per-query matmul runs *inline on the event loop* (§8.5), so this is
  really P8's slow-callback bar wearing a retrieval costume. Exceeding it stalls the loop.
* **~150 ms — the whole `recall` round trip.** Past this the tool "starts to be noticeable even
  with async function calling" (§6.6). This is a *felt* budget, not a loop-safety one, and it
  includes the query embedding, which the scan does not.

So this reports four numbers, each measured directly — **nothing is derived by subtraction**:

| number | what runs | budget |
|---|---|---|
| **scan** | `HybridRetriever.retrieve()` with the query vector pre-computed: FTS5 ∪ matmul ∪ §7.7 ranking | < 50 ms |
| **embed** | `Embedder.embed()` alone, plus the cores it burns | none published (§7.4) |
| **full recall** | `HybridRetriever.retrieve()` with the real embedder — what the `recall` tool costs | < ~150 ms |
| **FTS5** | `FactRepository.keyword_search()` alone — attribution for the scan number | none |

Both retrievers are the **real** `HybridRetriever` built through the composition root's own
`_build_retriever` over a real `SqliteFactRepo` (#127 AC-3: the real index path, not a bespoke
benchmark that accidentally measures something else). Only the *embedder* is substituted for the
scan measurement, by a fake that hands back a vector it was given — which is the honest way to
exclude the embed without pretending the rest is free.

**What this does not measure**, stated so no one reads it as coverage: retrieval *quality* (that is
#115's eval), the write path, or anything about a model deciding what to remember. And the embed
figure is only a §7.4 number when `[adapters] embedder = "local_minilm"` on the Pi — with the fake
it measures the fake, and the banner says so.

Usage (on the Pi; needs the ``pi``/``memory`` extra for numpy):

    /opt/avid/.venv/bin/python tools/spk3_retrieval_scan.py --config config/pi.toml
    /opt/avid/.venv/bin/python tools/spk3_retrieval_scan.py --config config/pi.toml \
        --counts 1000,5000,10000 --queries 50

It writes its synthetic corpus to a **throwaway database** and refuses to touch the one in
``[memory] db_path`` — the M7 gate (#129) wants that store to hold its 20 real facts and nothing
else.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from avid.adapters import SqliteFactRepo, pack_embedding
from avid.adapters.clock import SystemClock
from avid.core.config import Config, load_config
from avid.core.event_bus import AsyncioEventBus
from avid.core.ports import Clock, Embedder
from avid.domain import Fact
from avid.main import _build_embedder, _build_retriever

# SDS §7.7 (SDS:1663), verbatim: "Latency budget: <50 ms on the Pi … but if it exceeds ~150 ms the
# `recall` tool starts to be noticeable even with async function calling." These are SDS prose, not
# config keys — every *tunable* below (top_k, dimensions, weights, half-life) is read from the
# injected Config instead, because a literal that shadows a config value is drift with a delay fuse.
_SCAN_BUDGET_MS = 50.0
_NOTICEABLE_MS = 150.0

# Below this, nearest-rank P95 selects the last element — it *is* the maximum, and calling it a
# percentile overstates it in both directions (CLAUDE.md §7.1). The label changes, never the bar.
_MIN_FOR_PERCENTILE = 20

# Vocabulary for the synthetic corpus. Real words, so FTS5 has something to tokenise and the
# keyword branch does the work it would do in production; a corpus of "fact_0001" would measure
# an FTS5 index that never matches anything.
_SUBJECTS = (
    "Maya",
    "Ali",
    "Sam",
    "the dog",
    "my sister",
    "my manager",
    "the neighbour",
)
_VERBS = ("prefers", "dislikes", "always orders", "is allergic to", "asked about")
_OBJECTS = ("black coffee", "oat milk", "long walks", "early meetings", "loud music")
_CONTEXTS = (
    "on weekends",
    "before work",
    "in the evening",
    "at the office",
    "in winter",
)


def _synthetic_text(rng: random.Random, i: int) -> str:
    """One plausible fact sentence. ``i`` is folded in so no two rows collide exactly."""
    return (
        f"{rng.choice(_SUBJECTS)} {rng.choice(_VERBS)} {rng.choice(_OBJECTS)} "
        f"{rng.choice(_CONTEXTS)} ({i})"
    )


def _unit_vector(np: Any, rng: random.Random, dims: int) -> Any:
    """A pre-normalised random vector — §8.2's promise, which the scan's single matmul relies on."""
    vec = np.asarray([rng.gauss(0.0, 1.0) for _ in range(dims)], dtype=np.float32)
    return vec / float(np.sqrt((vec * vec).sum()))


class _PrecomputedEmbedder:
    """An :class:`~avid.core.ports.Embedder` that returns a vector handed to it in advance.

    Not a mock of the embedder — it is a legitimate fake standing in for a component whose cost is
    measured *separately*, so the scan number contains the scan and nothing else. Substituting it is
    the only way to time FTS ∪ matmul ∪ ranking through the real ``retrieve()`` without also timing
    a 90 MB transformer.
    """

    def __init__(self, *, dimensions: int) -> None:
        self._dimensions = dimensions
        self._vector: Sequence[float] = ()

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def load(self, vector: Sequence[float]) -> None:
        self._vector = vector

    async def embed(self, text: str) -> Sequence[float]:
        return self._vector


def _tail(samples: list[float]) -> tuple[float, str]:
    """The upper-tail statistic and **the name it is entitled to** (CLAUDE.md §7.1).

    Nearest rank picks ``ceil(0.95n)``, which is ``n`` for every ``n < 20``: below twenty samples a
    "P95" is arithmetically the maximum, and a real P95 tolerates 1 in 20 above the line where a
    maximum tolerates none. Fix the label, never the threshold.
    """
    ordered = sorted(samples)
    if len(ordered) < _MIN_FOR_PERCENTILE:
        return ordered[-1], f"worst of {len(ordered)}"
    return ordered[math.ceil(0.95 * len(ordered)) - 1], "P95"


def _p50(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[math.ceil(0.5 * len(ordered)) - 1]


async def _populate(
    repo: SqliteFactRepo, *, np: Any, count: int, dims: int, seed: int
) -> float:
    """Insert ``count`` synthetic facts with pre-normalised vectors. Returns wall seconds."""
    rng = random.Random(seed)
    now = int(time.time())
    started = time.monotonic()
    for i in range(count):
        fact = Fact(
            id=0,  # assigned by the repository on insert
            text=_synthetic_text(rng, i),
            kind="other",
            importance=rng.randint(1, 10),
            created_at=now - rng.randint(0, 365 * 24 * 3600),
            # Spread over the §7.7 half-life so recency scoring has a real spread to normalise.
            last_accessed_at=now - rng.randint(0, 60 * 24 * 3600),
        )
        await repo.add(fact, embedding=pack_embedding(_unit_vector(np, rng, dims)))
    return time.monotonic() - started


async def _measure(
    *,
    config: Config,
    repo: SqliteFactRepo,
    embedder: Embedder,
    bus: AsyncioEventBus,
    clock: Clock,
    np: Any,
    queries: int,
    seed: int,
) -> dict[str, Any]:
    """Time the four quantities over ``queries`` distinct queries against the built index."""
    dims = config.memory.dimensions
    rng = random.Random(seed + 1)
    texts = [_synthetic_text(rng, 10_000 + i) for i in range(queries)]

    scan_embedder = _PrecomputedEmbedder(dimensions=dims)
    scan_retriever = _build_retriever(
        config, repo=repo, embedder=scan_embedder, bus=bus, clock=clock
    )
    full_retriever = _build_retriever(
        config, repo=repo, embedder=embedder, bus=bus, clock=clock
    )

    rebuild_started = time.monotonic()
    await scan_retriever.rebuild()
    rebuild_s = time.monotonic() - rebuild_started
    await full_retriever.rebuild()

    def ms_since(ns: int) -> float:
        return (time.monotonic_ns() - ns) / 1_000_000.0

    # ── embed alone, with the cores it burns (#168 AC-3 / #127 AC-4) ──
    # cores = CPU-seconds across every thread / wall-seconds. An ONNX pool left at its default
    # spins one thread per core and shows up here as ~N; a bounded pool shows up as its cap.
    embed_ms: list[float] = []
    vectors: list[Sequence[float]] = []
    cpu0, wall0 = time.process_time(), time.monotonic()
    for text in texts:
        started_ns = time.monotonic_ns()
        vectors.append(await embedder.embed(text))
        embed_ms.append(ms_since(started_ns))
    cores = (time.process_time() - cpu0) / max(time.monotonic() - wall0, 1e-9)

    # ── the scan: FTS5 ∪ matmul ∪ §7.7 ranking, with the vector already in hand ──
    scan_ms: list[float] = []
    for text, vector in zip(texts, vectors, strict=True):
        scan_embedder.load(vector)
        started_ns = time.monotonic_ns()
        await scan_retriever.retrieve(text)
        scan_ms.append(ms_since(started_ns))

    # ── FTS5 alone, so the scan number can be attributed rather than guessed at ──
    fts_ms: list[float] = []
    for text in texts:
        started_ns = time.monotonic_ns()
        await repo.keyword_search(text, limit=config.memory.top_k)
        fts_ms.append(ms_since(started_ns))

    # ── the whole recall: what the tool actually costs the conversation ──
    full_ms: list[float] = []
    for text in texts:
        started_ns = time.monotonic_ns()
        await full_retriever.retrieve(text)
        full_ms.append(ms_since(started_ns))

    return {
        "rebuild_s": rebuild_s,
        "embed_ms": embed_ms,
        "cores": cores,
        "scan_ms": scan_ms,
        "fts_ms": fts_ms,
        "full_ms": full_ms,
    }


def _report(count: int, m: dict[str, Any], *, real_embedder: bool) -> bool:
    """Print every measurement, then decide. Returns True iff the graded scan is within budget.

    Every criterion reports **before** any verdict is decided (CLAUDE.md §7.1): a failing scan must
    not hide a passing recall figure, and vice versa.
    """
    scan_p50 = _p50(m["scan_ms"])
    scan_tail, scan_label = _tail(m["scan_ms"])
    full_tail, full_label = _tail(m["full_ms"])
    embed_tail, embed_label = _tail(m["embed_ms"])
    fts_tail, _ = _tail(m["fts_ms"])

    print(f"\n=== N = {count:,} facts ===")
    print(f"index rebuild (§8.5 boot cost): {m['rebuild_s'] * 1000:.0f} ms")
    print(f"{'quantity':<34}{'p50':>10}{'tail':>12}   budget")
    print("-" * 74)
    print(
        f"{'scan (FTS ∪ matmul ∪ ranking)':<34}{scan_p50:>9.2f}ms"
        f"{scan_tail:>11.2f}ms   < {_SCAN_BUDGET_MS:.0f} ms  ({scan_label})"
    )
    print(
        f"{'FTS5 keyword_search (a part of it)':<34}{_p50(m['fts_ms']):>9.2f}ms"
        f"{fts_tail:>11.2f}ms   —"
    )
    print(
        f"{'embed (one query)':<34}{_p50(m['embed_ms']):>9.2f}ms"
        f"{embed_tail:>11.2f}ms   —          ({embed_label})"
    )
    print(
        f"{'full recall (embed + scan)':<34}{_p50(m['full_ms']):>9.2f}ms"
        f"{full_tail:>11.2f}ms   < {_NOTICEABLE_MS:.0f} ms  ({full_label})"
    )
    print(f"{'cores busy during embed':<34}{m['cores']:>9.2f}")
    # Each row is its own measurement over the same queries, so the p50s nest but the TAILS do not:
    # FTS5's worst query and the scan's worst query need not be the same query, and FTS5's tail can
    # read higher than the scan tail it is part of. Reading the tail column as a breakdown is the
    # arithmetic mistake this line exists to prevent.
    print(
        "  (tails are per-quantity — the worst query differs per row, so they do not nest)"
    )

    scan_ok = scan_tail < _SCAN_BUDGET_MS
    full_ok = full_tail < _NOTICEABLE_MS
    print()
    print(
        f"  scan < {_SCAN_BUDGET_MS:.0f} ms (§7.7, and P8's bar since it runs inline): "
        f"{'PASS' if scan_ok else 'FAIL'}"
    )
    print(
        f"  full recall < {_NOTICEABLE_MS:.0f} ms (§7.7 'noticeable'): "
        f"{'PASS' if full_ok else 'FAIL'}"
    )
    if not full_ok:
        print(
            "    ^ note where the time went before reaching for the index: if `embed` dominates,\n"
            "      this is §7.4's model cost, not ADR-005's scan, and int8 quantising the vectors\n"
            "      would not move it."
        )
    if not real_embedder:
        print(
            "\n  ⚠️  [adapters] embedder is NOT local_minilm — the embed and full-recall rows above\n"
            "      measure the FAKE embedder and say nothing about §7.4 or the recall tool on the\n"
            "      Pi. Only the scan and FTS rows are meaningful in this configuration."
        )
    return scan_ok


async def _run(args: argparse.Namespace, *, config: Config, db_path: Path) -> int:
    import numpy as np

    real_embedder = config.adapters.embedder == "local_minilm"
    print(
        f"SPK-3 — #127. dims={config.memory.dimensions} top_k={config.memory.top_k} "
        f"embedder={config.adapters.embedder} queries={args.queries}"
    )
    print(f"corpus db: {db_path} (throwaway; [memory] db_path is untouched)")

    all_ok = True
    try:
        for count in args.counts:
            bus = AsyncioEventBus(clock=SystemClock())
            # retrieve() publishes memory.recall_completed (§9.1.3) and the bus refuses to publish
            # before start(). A real bus with no subscribers, rather than a stub: the publish is
            # part of what a recall costs, and excluding it would measure a retrieval we do not run.
            await bus.start()
            clock: Clock = SystemClock()
            repo = SqliteFactRepo(db_path=db_path, clock=clock)
            try:
                print(f"\npopulating {count:,} facts…", flush=True)
                insert_s = await _populate(
                    repo,
                    np=np,
                    count=count,
                    dims=config.memory.dimensions,
                    seed=args.seed,
                )
                print(f"  inserted in {insert_s:.1f} s")
                m = await _measure(
                    config=config,
                    repo=repo,
                    embedder=_build_embedder(config),
                    bus=bus,
                    clock=clock,
                    np=np,
                    queries=args.queries,
                    seed=args.seed,
                )
                all_ok = _report(count, m, real_embedder=real_embedder) and all_ok
            finally:
                await repo.aclose()
                await bus.stop()
                await asyncio.to_thread(db_path.unlink, True)
    finally:
        pass

    print(
        "\nFold these into SDS §7.7's scale table (#127 AC-5): which action, at which N, on THIS\n"
        "hardware. If the numbers hold, ADR-005 is confirmed and nothing changes."
    )
    return 0 if all_ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--config", required=True, help="path to the TOML config to run under"
    )
    parser.add_argument(
        "--counts",
        default="5000",
        help="comma-separated corpus sizes (#127 AC-1 asks for 5000; a sweep populates §7.7's table)",
    )
    parser.add_argument(
        "--queries",
        type=int,
        default=50,
        help=f"queries per size; below {_MIN_FOR_PERCENTILE} the tail is labelled a maximum, not a P95",
    )
    parser.add_argument("--seed", type=int, default=127, help="corpus/query seed")
    return parser


def main(argv: list[str] | None = None) -> int:
    # The report uses ∪/§/⚠ and the Pi's console is UTF-8, but a Windows dev box defaults to cp1252
    # and would die mid-table with a UnicodeEncodeError — losing the run, not just the character.
    # A bench that only runs where the encoding happens to suit it is one you cannot smoke locally.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)
    args.counts = [int(c) for c in str(args.counts).split(",") if c.strip()]
    config = load_config(args.config)

    # The synthetic corpus goes in a throwaway file, never [memory] db_path: #129 wants that store
    # to hold the gate's 20 real facts and nothing else, and a 5,000-row corpus written into it
    # would be discovered at the gate, not here. Resolved before the loop starts, because these are
    # blocking filesystem calls and P8 says they do not belong on it.
    workdir = Path(tempfile.mkdtemp(prefix="spk3-"))
    db_path = workdir / "spk3.db"
    if db_path.resolve() == Path(config.memory.db_path).expanduser().resolve():
        print(f"refusing to write the synthetic corpus into {config.memory.db_path}")
        return 2
    try:
        return asyncio.run(_run(args, config=config, db_path=db_path))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
