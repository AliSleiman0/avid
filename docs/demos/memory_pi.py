"""On-Pi M7 gate harness (#129) — verify the memory, at the database, against a declared corpus.

The robot holds the conversation; this measures what the conversation left behind. That division is
deliberate: **any "did it work" number must come from the store, not from what we handed the store**
(the M4 lesson — a harness that timed its own submissions printed PASS over a mute robot). So every
criterion here reads SQLite or drives the real `HybridRetriever`, and none of them trusts how the
spoken answer sounded.

What each mode grades, and what it deliberately does not:

* **inventory (AC-1)** — which declared facts landed as rows, and whether extraction was *silent*.
  The row count is **recorded, not graded**: extraction rate is the model's instruction-following
  (§7.6) and it is AC-2 that grades it, once, with a denominator. Silence is graded, because the
  instructions are ours. Read from the `episodes` transcript, so it survives a restart.
* **recall (AC-2)** — every declared fact's probe query, run through the **real retriever over the
  real store**, judged at the *fact*: a fluent answer naming the wrong row is a miss. Denominator is
  the **declared 20**, so an extraction miss counts as a recall miss (O2 is "recall of stated facts").
* **semantic (AC-3)** — the queries the pre-injection block cannot cover, including the proper-noun
  branch FTS5 exists for.
* **supersede (AC-4)** — the old row carries its supersession pointer, the new row wins the present,
  and the old one is still queryable as history.
* **forget (AC-5)** — the row is gone, no FTS5 index entry outlives it, and it never comes back from
  retrieval. A hard cascading DELETE checked in the schema and in behaviour, never inferred from a
  polite refusal to mention it. The index half has to be behavioural: ``facts_fts`` is
  **external-content**, so a `SELECT` reads through to a row that is already deleted and reports a
  surviving index entry as clean.

**There are two phases and no mode that grades both — that is the whole design.** Supersession and
forgetting *destroy the evidence AC-2 is scored on*: the superseded coffee row leaves `fetch_live`
and the forgotten row leaves the database entirely, so a recall scored afterwards reports two misses
that are not misses, and it reads exactly like a model failure. The store cannot be in both states
at once, so the harness runs twice:

1. State the 20 facts → ``systemctl restart`` → ``--mode recall`` (**AC-1/2/3**).
2. Then the supersession and "forget that" turns → ``--mode mutations`` (**AC-4/5**).

The first version of this file had a single ``--mode all``, and its own baseline test caught the
flaw: on a store that had been through both conversations, a *correct* run scored 2/4 recall.

**It never writes.** Probes go through `Retriever.retrieve`, which does not touch `access_count`;
the store this leaves behind is the one #129 keeps for O2's 30-day re-measure.

Usage on the Pi (service **stopped**, per PI_OPERATIONS §1):

    /opt/avid/.venv/bin/python docs/demos/memory_pi.py --config /etc/robot/config.toml
    /opt/avid/.venv/bin/python docs/demos/memory_pi.py --config /etc/robot/config.toml \
        --mode recall --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from avid.adapters import SqliteFactRepo
from avid.adapters.clock import SystemClock
from avid.core.config import Config, load_config
from avid.core.event_bus import AsyncioEventBus
from avid.core.ports import Clock
from avid.domain import Fact
from avid.main import _build_embedder, _build_retriever

# O2, PMP §114: "≥95% recall of stated facts". Over the declared 20 that is 19 — one miss fails.
# Settled on #129 before the run; if it fails, M7 seals with the gap recorded (as M5 did with O1's
# P95) rather than this number moving.
_O2_RECALL = 0.95

# Phrases that would mean the model narrated a write instead of performing it silently (§7.6).
# Matched case-insensitively against assistant transcript text.
_ANNOUNCEMENTS = (
    "i'll remember",
    "i will remember",
    "i've remembered",
    "i have remembered",
    "noted",
    "saved that",
    "i've saved",
    "storing that",
    "added to my memory",
    "committing that to memory",
)

_DEFAULT_SCRIPT = Path(__file__).resolve().parent / "m7_evidence" / "facts.json"


@dataclass
class _Criterion:
    """One reported line. Held rather than printed so **every criterion reports before any verdict
    is decided** — hiding a passing check behind an unrelated failure is the sibling of passing on
    silence (CLAUDE.md §7.1)."""

    ac: str
    name: str
    passed: bool | None  # None = recorded, not graded
    detail: str
    rows: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _matches(row_text: str, needles: list[str]) -> bool:
    """A stored row counts as a declared fact when it contains every declared substring.

    Loose on phrasing because §7.6 stores the user's own words and the model chooses them; strict on
    content, so "I drink tea" can never be scored as the coffee fact.
    """
    haystack = _norm(row_text)
    return all(_norm(n) in haystack for n in needles)


def _find(facts: list[Fact], needles: list[str]) -> Fact | None:
    for fact in facts:
        if _matches(fact.text, needles):
            return fact
    return None


async def _all_rows(repo: SqliteFactRepo) -> list[Fact]:
    return list(await repo.fetch_live())


def _raw(db_path: Path) -> sqlite3.Connection:
    """A read-only connection for the checks that must see the *schema*, not the port.

    AC-5 is about the FTS5 shadow and the embedding BLOB — neither is visible through
    ``FactRepository``, and a delete that left them behind would pass every port-level check while
    the data was still on disk. Opened read-only so the audit cannot be what changed the store.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ── AC-1 ─────────────────────────────────────────────────────────────────────────────────────


def _inventory(
    script: dict[str, Any], facts: list[Fact], db_path: Path
) -> list[_Criterion]:
    declared = script["facts"]
    landed = [d for d in declared if _find(facts, d["match"]) is not None]
    missing = [d for d in declared if _find(facts, d["match"]) is None]

    out = [
        _Criterion(
            "AC-1",
            f"extraction: {len(landed)}/{len(declared)} declared facts landed as rows",
            None,  # recorded, not graded — AC-2 grades this, once, with a denominator
            "recorded, not graded (settled on #129: extraction rate is AC-2's to grade)",
            rows=[f"no row for '{d['key']}': {d['say']}" for d in missing],
        )
    ]

    with _raw(db_path) as conn:
        transcripts = [
            r["transcript"] or ""
            for r in conn.execute("SELECT transcript FROM episodes").fetchall()
        ]
    blob = _norm(" ".join(transcripts))
    heard = [phrase for phrase in _ANNOUNCEMENTS if phrase in blob]
    out.append(
        _Criterion(
            "AC-1",
            "extraction was silent — no write announced in the transcript",
            not heard,
            "no announcement phrases found"
            if not heard
            else f"announced: {', '.join(heard)}",
        )
    )
    if not transcripts:
        out.append(
            _Criterion(
                "AC-1",
                "transcript available to check silence against",
                False,
                "NO EPISODES IN THE STORE — the silence check above passed on an empty string, "
                "which is not evidence of silence. Confirm [adapters] episode_store is real and "
                "the conversation ran under the service.",
            )
        )
    return out


# ── AC-2 / AC-3 ──────────────────────────────────────────────────────────────────────────────


async def _probe(
    retriever: Any, query: str, facts: list[Fact], needles: list[str]
) -> tuple[bool, float]:
    """Run one probe through the real retriever. Returns (the expected fact came back, ms)."""
    expected = _find(facts, needles)
    started = time.monotonic_ns()
    ids = await retriever.retrieve(query)
    elapsed_ms = (time.monotonic_ns() - started) / 1_000_000.0
    if expected is None:
        return False, elapsed_ms  # never stored ⇒ cannot be recalled ⇒ a miss, per AC-2
    return expected.id in tuple(ids), elapsed_ms


async def _recall(
    script: dict[str, Any], facts: list[Fact], retriever: Any
) -> list[_Criterion]:
    declared = script["facts"]
    hits: list[str] = []
    misses: list[str] = []
    for d in declared:
        ok, _ = await _probe(retriever, d["probe"], facts, d["match"])
        (hits if ok else misses).append(d["key"])

    rate = len(hits) / len(declared)
    return [
        _Criterion(
            "AC-2",
            f"recall of stated facts: {len(hits)}/{len(declared)} = {rate:.0%}",
            rate >= _O2_RECALL,
            f"O2 bar is ≥{_O2_RECALL:.0%} of the {len(declared)} STATED facts — an extraction "
            f"miss counts as a recall miss",
            rows=[f"missed '{k}'" for k in misses],
        )
    ]


async def _semantic(
    script: dict[str, Any], facts: list[Fact], retriever: Any
) -> list[_Criterion]:
    out: list[_Criterion] = []
    by_key = {d["key"]: d for d in script["facts"]}
    for probe in script["semantic_probes"]:
        target = by_key[probe["expect"]]
        ok, ms = await _probe(retriever, probe["query"], facts, target["match"])
        label = "proper-noun " if probe.get("proper_noun") else ""
        out.append(
            _Criterion(
                "AC-3",
                f"{label}query returns the right fact: {probe['query']!r}",
                ok,
                f"expected '{probe['expect']}', {ms:.0f} ms — judged at the returned fact, "
                f"not at the phrasing",
            )
        )
    if not any(p.get("proper_noun") for p in script["semantic_probes"]):
        out.append(
            _Criterion(
                "AC-3",
                "at least one proper-noun query",
                False,
                "none declared in the script",
            )
        )
    return out


# ── AC-4 ─────────────────────────────────────────────────────────────────────────────────────


async def _supersede(
    script: dict[str, Any], facts: list[Fact], retriever: Any, db_path: Path
) -> list[_Criterion]:
    spec = script["supersession"]
    by_key = {d["key"]: d for d in script["facts"]}
    old_spec = by_key[spec["supersedes"]]

    with _raw(db_path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM facts").fetchall()]
    old = next((r for r in rows if _matches(r["text"], old_spec["match"])), None)
    new = next((r for r in rows if _matches(r["text"], spec["match"])), None)

    out = [
        _Criterion(
            "AC-4",
            "the superseded row carries its pointer (superseded_by + superseded_at)",
            old is not None
            and old["superseded_by"] is not None
            and old["superseded_at"] is not None,
            "not found"
            if old is None
            else f"id={old['id']} superseded_by={old['superseded_by']}",
        ),
        _Criterion(
            "AC-4",
            "the new row is live (not itself superseded)",
            new is not None and new["superseded_by"] is None,
            "not found" if new is None else f"id={new['id']}",
        ),
    ]

    present_ok, _ = await _probe(retriever, spec["probe_present"], facts, spec["match"])
    out.append(
        _Criterion(
            "AC-4",
            f"the present is uncontaminated: {spec['probe_present']!r} returns the new fact",
            present_ok,
            f"expected a row matching {spec['match']}",
        )
    )

    # History: fetch_live excludes superseded rows, so the old fact is checked in the schema —
    # "still queryable" is a property of the store, and retrieval deliberately does not surface it.
    out.append(
        _Criterion(
            "AC-4",
            "history is still queryable — the superseded row was not deleted",
            old is not None,
            "supersession is an epistemics operation; only AC-5's forget removes data (§7.8/§7.10)",
        )
    )
    return out


# ── AC-5 ─────────────────────────────────────────────────────────────────────────────────────


async def _forget(
    script: dict[str, Any], repo: SqliteFactRepo, retriever: Any, db_path: Path
) -> list[_Criterion]:
    """AC-5, checked three ways, because one way is not enough on this schema.

    ``facts_fts`` is an **external-content** FTS5 table (``content='facts'``), so
    ``SELECT text FROM facts_fts`` reads *through* to ``facts`` — a stale index entry whose content
    row is gone reports as empty rather than as an orphan. Asking the index by SELECT would
    therefore call a leak clean. The honest question is behavioural: **does the deleted text still
    match a search, and does it still come back from retrieval?**
    """
    spec = script["forget"]
    by_key = {d["key"]: d for d in script["facts"]}
    target = by_key[spec["target"]]
    term = " ".join(target["match"])

    with _raw(db_path) as conn:
        rows = [
            dict(r)
            for r in conn.execute("SELECT id, text, embedding FROM facts").fetchall()
        ]
    live_ids = {r["id"] for r in rows}
    row = next((r for r in rows if _matches(r["text"], target["match"])), None)

    # Query the INDEX directly, not through FactRepository.keyword_search — that method JOINs
    # `facts` (to filter superseded rows), so it can only ever return ids that still have a live
    # row. Asking it about orphans is a check that cannot fail, which is worse than no check.
    # `SELECT rowid ... MATCH` reads the index alone and does surface an entry whose content row
    # is gone. An id here that `facts` does not have IS the leak the facts_fts_ad trigger prevents.
    match = " ".join(f'"{n}"' for n in target["match"])
    with _raw(db_path) as conn:
        hits = [
            int(r["rowid"])
            for r in conn.execute(
                "SELECT rowid FROM facts_fts WHERE facts_fts MATCH ?", (match,)
            ).fetchall()
        ]
    orphans = [i for i in hits if i not in live_ids]

    recalled = [
        r["text"]
        for r in rows
        if r["id"] in tuple(await retriever.retrieve(target["probe"]))
        and _matches(r["text"], target["match"])
    ]

    return [
        _Criterion(
            "AC-5",
            f"the forgotten row is gone from `facts` ('{spec['target']}')",
            row is None,
            "hard cascading DELETE, not supersession — a rights operation (§7.10)"
            if row is None
            else f"STILL PRESENT as id={row['id']}",
        ),
        _Criterion(
            "AC-5",
            "no FTS5 index entry survives the deleted row",
            not orphans,
            f"keyword_search({term!r}) returns only live rows"
            if not orphans
            else f"orphaned index rowid(s) {orphans} — searchable text left on disk after the "
            f"row was deleted (the facts_fts_ad trigger did not fire)",
        ),
        _Criterion(
            "AC-5",
            "it never reappears in retrieval",
            not recalled,
            f"probe {target['probe']!r} returns nothing matching the forgotten fact"
            if not recalled
            else f"came back: {recalled}",
        ),
    ]


# ── report ───────────────────────────────────────────────────────────────────────────────────


def _report(criteria: list[_Criterion]) -> int:
    print("\n" + "=" * 78)
    print("M7 gate — #129, verified at the database")
    print("=" * 78)
    graded = 0
    failed = 0
    for c in criteria:
        if c.passed is None:
            mark = "····"
        elif c.passed:
            mark = "PASS"
            graded += 1
        else:
            mark = "FAIL"
            graded += 1
            failed += 1
        print(f"[{mark}] {c.ac}  {c.name}")
        if c.detail:
            print(f"        {c.detail}")
        for row in c.rows:
            print(f"          - {row}")
    print("-" * 78)
    print(f"{graded - failed}/{graded} graded criteria passed; {failed} failed.")
    if failed:
        print(
            "\nA failure here is a result, not an accident — record it on #129 and seal with the\n"
            "gap named, the way M5 sealed with O1's P95 unmet. Do not move a bar to fit a number."
        )
    return 1 if failed else 0


async def _run(
    args: argparse.Namespace, *, config: Config, script: dict[str, Any], db_path: Path
) -> int:
    clock: Clock = SystemClock()
    bus = AsyncioEventBus(clock=clock)
    await bus.start()
    repo = SqliteFactRepo(db_path=db_path, clock=clock)
    try:
        facts = await _all_rows(repo)
        retriever = _build_retriever(
            config, repo=repo, embedder=_build_embedder(config), bus=bus, clock=clock
        )
        await retriever.rebuild()

        print(f"store: {db_path}  ({len(facts)} live facts)")
        print(f"script: {args.script}  ({len(script['facts'])} declared)")
        if config.adapters.embedder != "local_minilm":
            print(
                "\n⚠️  [adapters] embedder is NOT local_minilm — recall is being judged with the\n"
                "    FAKE embedder, whose geometry is bag-of-words. AC-2/AC-3 results below do not\n"
                "    describe the robot on the Pi."
            )

        criteria: list[_Criterion] = []
        mode = args.mode
        if mode in ("recall", "inventory"):
            criteria += _inventory(script, facts, db_path)
        if mode in ("recall",):
            criteria += await _recall(script, facts, retriever)
        if mode in ("recall", "semantic"):
            criteria += await _semantic(script, facts, retriever)
        if mode in ("mutations", "supersede"):
            criteria += await _supersede(script, facts, retriever, db_path)
        if mode in ("mutations", "forget"):
            criteria += await _forget(script, repo, retriever, db_path)

        code = _report(criteria)
        if args.json:
            payload = json.dumps(
                [
                    {
                        "ac": c.ac,
                        "name": c.name,
                        "passed": c.passed,
                        "detail": c.detail,
                        "rows": c.rows,
                    }
                    for c in criteria
                ],
                indent=2,
            )
            await asyncio.to_thread(
                Path(args.json).write_text, payload, encoding="utf-8"
            )
            print(f"\nwrote {args.json}")
        return code
    finally:
        await repo.aclose()
        await bus.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="M7 gate harness (#129) - verify the memory at the database"
    )
    parser.add_argument(
        "--config", required=True, help="the TOML config the robot ran under"
    )
    parser.add_argument(
        "--script", default=str(_DEFAULT_SCRIPT), help="declared fact corpus (JSON)"
    )
    parser.add_argument(
        "--mode",
        default="recall",
        choices=("recall", "mutations", "inventory", "semantic", "supersede", "forget"),
        help=(
            "'recall' (AC-1/2/3) runs after the restart, BEFORE the supersession and forget turns; "
            "'mutations' (AC-4/5) runs after them. There is deliberately no mode that grades both."
        ),
    )
    parser.add_argument(
        "--json", default=None, help="also write the criteria to this JSON file"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    script = json.loads(Path(args.script).read_text(encoding="utf-8"))

    # Resolved here, off the loop: these are blocking filesystem calls, and P8's rule does not stop
    # applying because the file is small.
    db_path = Path(config.memory.db_path).expanduser()
    if not db_path.is_file():
        print(f"no store at {db_path} — has the robot run under this config?")
        return 2
    return asyncio.run(_run(args, config=config, script=script, db_path=db_path))


if __name__ == "__main__":
    raise SystemExit(main())
