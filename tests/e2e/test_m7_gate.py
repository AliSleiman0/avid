"""The M7 gate harness's own pass/fail logic (#129 AC-8).

`docs/demos/memory_pi.py` is what decides whether M7 held. The M4 gate taught this project what an
untested reporter is worth: its pass/fail logic had no test, and it printed **PASS over a mute
robot** (#91). So this suite does to the M7 harness what nobody did to the M4 one — builds real
stores in real SQLite, breaks one property at a time, and asserts the harness *notices*.

Every test here is a **neuter**: a store that is correct except for one thing. A harness that passes
all of these while failing to fail on a broken store would be worse than no harness, because it
would carry a milestone's name.

Runs everywhere — real `SqliteFactRepo`, real `HybridRetriever`, the stdlib `FakeEmbedder` (P6), no
Pi, no key, no model blob.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from avid.adapters import SqliteFactRepo, pack_embedding
from avid.adapters.clock import SystemClock
from avid.adapters.embedder import FakeEmbedder
from avid.core.config import Config, load_config
from avid.core.event_bus import AsyncioEventBus
from avid.domain import Fact

_DEMO_MODULE = "avid_demo_memory_pi"


def _load_memory_pi() -> ModuleType:
    """Import ``docs/demos/memory_pi.py`` by path — ``docs/demos`` is not a package (see
    ``test_m4_gate._load_audio_pi``, same reasoning and the same reason it matters)."""
    if (cached := sys.modules.get(_DEMO_MODULE)) is not None:
        return cached
    path = Path(__file__).resolve().parents[2] / "docs" / "demos" / "memory_pi.py"
    spec = importlib.util.spec_from_file_location(_DEMO_MODULE, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_DEMO_MODULE] = module
    spec.loader.exec_module(module)
    return module


# A miniature corpus with the same *shape* as the shipped one (docs/demos/m7_evidence/facts.json):
# four declared facts, a proper-noun probe, a supersession pair and a forget target. Four rather
# than twenty so an "all facts present" store is one line, and so the recall arithmetic is easy to
# read: with n=4 the O2 bar of 95% means all four.
_SCRIPT: dict[str, Any] = {
    "facts": [
        {
            "key": "coffee",
            "kind": "preference",
            "say": "I drink black coffee",
            "match": ["coffee"],
            "probe": "what do I drink?",
        },
        {
            "key": "sister",
            "kind": "relationship",
            "say": "My sister Maya is a doctor",
            "match": ["maya"],
            "probe": "what did I say about my sister?",
        },
        {
            "key": "dog",
            "kind": "relationship",
            "say": "The dog is called Biscuit",
            "match": ["biscuit"],
            "probe": "what is the dog called?",
        },
        {
            "key": "run",
            "kind": "routine",
            "say": "I run on Tuesdays",
            "match": ["run", "tuesday"],
            "probe": "when do I run?",
        },
    ],
    "semantic_probes": [
        {
            "query": "what did I say about my sister?",
            "expect": "sister",
            "proper_noun": False,
        },
        {"query": "tell me about Biscuit", "expect": "dog", "proper_noun": True},
    ],
    "supersession": {
        "say": "I've switched to tea",
        "supersedes": "coffee",
        "match": ["tea"],
        "probe_present": "what do I drink?",
        "probe_history": "what did I used to drink?",
    },
    "forget": {"say": "forget the dog", "target": "dog"},
}


async def _store(
    tmp_path: Path,
    *,
    facts: Sequence[str],
    transcript: str | None = "so, tell me more about that",
    supersede: tuple[str, str] | None = ("coffee", "I have switched to tea"),
    forget: str | None = "biscuit",
) -> Path:
    """Build a real store: insert ``facts``, optionally supersede a pair and delete a row.

    Returns the db path. Everything goes through :class:`SqliteFactRepo`, so the FTS5 triggers and
    the schema constraints are the real ones — which is the only way the AC-5 orphan test below can
    mean anything.
    """
    db_path = tmp_path / "m7.db"
    clock = SystemClock()
    repo = SqliteFactRepo(db_path=db_path, clock=clock)
    embedder = FakeEmbedder(dimensions=384)
    now = int(time.time())
    ids: dict[str, int] = {}
    try:
        for text in facts:
            fact = Fact(
                id=0,
                text=text,
                kind="other",
                importance=5,
                created_at=now,
                last_accessed_at=now,
            )
            ids[text] = await repo.add(
                fact, embedding=pack_embedding(await embedder.embed(text))
            )

        if supersede is not None:
            old_needle, new_text = supersede
            old_id = next(i for t, i in ids.items() if old_needle in t.lower())
            new_id = await repo.add(
                Fact(
                    id=0,
                    text=new_text,
                    kind="other",
                    importance=5,
                    created_at=now,
                    last_accessed_at=now,
                ),
                embedding=pack_embedding(await embedder.embed(new_text)),
            )
            await repo.mark_superseded(old_id, new_id, at=now)

        if forget is not None:
            target = next(i for t, i in ids.items() if forget in t.lower())
            await repo.delete(target)
    finally:
        await repo.aclose()

    if transcript is not None:
        await asyncio.to_thread(_write_episode, db_path, transcript, now)
    return db_path


def _sql(db_path: Path, *statements: str) -> None:
    """Run raw SQL off the loop.

    Synchronous sqlite3 in an async test body is blocking I/O on the event loop — the thing P8
    exists to catch — and it eats the margin of every marginal test that runs after it. Threading
    it costs one line and keeps this suite from being the reason someone else's gate flakes.
    """
    with sqlite3.connect(db_path) as conn:
        for statement in statements:
            conn.execute(statement)
        conn.commit()


def _write_episode(db_path: Path, transcript: str, now: int) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO episodes(correlation_id, started_at, turn_count, transcript) "
            "VALUES (?, ?, ?, ?)",
            ("00000000-0000-0000-0000-000000000000", now, 4, transcript),
        )
        conn.commit()


def _config(db_path: Path) -> Config:
    """`config/sim.toml` pointed at the store — the fake embedder, so this runs in CI (P6)."""
    base = load_config("config/sim.toml")
    return base.model_copy(
        update={"memory": base.memory.model_copy(update={"db_path": str(db_path)})}
    )


async def _run(
    db_path: Path, script: dict[str, Any] | None = None, *, phase: str = "recall"
) -> list[Any]:
    """Drive one phase of the harness and return its criteria.

    ``phase`` exists because the harness has no mode that grades both: a store that has been
    superseded and forgotten cannot also be the store AC-2 is scored against.
    """
    memory_pi = _load_memory_pi()
    config = _config(db_path)
    clock = SystemClock()
    bus = AsyncioEventBus(clock=clock)
    await bus.start()
    repo = SqliteFactRepo(db_path=db_path, clock=clock)
    try:
        from avid.main import _build_retriever

        facts = list(await repo.fetch_live())
        retriever = _build_retriever(
            config,
            repo=repo,
            embedder=FakeEmbedder(dimensions=384),
            bus=bus,
            clock=clock,
        )
        await retriever.rebuild()
        spec = script if script is not None else _SCRIPT
        if phase == "recall":
            criteria = list(memory_pi._inventory(spec, facts, db_path))
            criteria += await memory_pi._recall(spec, facts, retriever)
            criteria += await memory_pi._semantic(spec, facts, retriever)
        else:
            criteria = list(await memory_pi._supersede(spec, facts, retriever, db_path))
            criteria += await memory_pi._forget(spec, repo, retriever, db_path)
        return criteria
    finally:
        await repo.aclose()
        await bus.stop()


def _failed(criteria: list[Any]) -> list[str]:
    return [f"{c.ac} {c.name}" for c in criteria if c.passed is False]


def _by_ac(criteria: list[Any], ac: str) -> list[Any]:
    return [c for c in criteria if c.ac == ac]


_ALL_FACTS = (
    "I drink black coffee",
    "My sister Maya is a doctor",
    "The dog is called Biscuit",
    "I run on Tuesdays",
)


async def test_an_intact_store_passes_the_recall_phase(tmp_path: Path) -> None:
    """Baseline one: all 4 facts stored, nothing superseded, nothing forgotten — the state the
    store is in when AC-1/2/3 are scored. If this fails, the neuters below prove nothing."""
    db = await _store(tmp_path, facts=_ALL_FACTS, supersede=None, forget=None)
    criteria = await _run(db, phase="recall")
    assert _failed(criteria) == []
    assert _load_memory_pi()._report(criteria) == 0


async def test_a_mutated_store_passes_the_mutation_phase(tmp_path: Path) -> None:
    """Baseline two: after the supersession and forget turns — the state AC-4/5 are scored in."""
    db = await _store(tmp_path, facts=_ALL_FACTS)
    criteria = await _run(db, phase="mutations")
    assert _failed(criteria) == []
    assert _load_memory_pi()._report(criteria) == 0


async def test_recall_scored_on_a_mutated_store_undercounts_which_is_why_phases_exist(
    tmp_path: Path,
) -> None:
    """The defect that produced the two-phase design, kept as a test so it cannot come back.

    On a store that has been through *both* conversations, a perfectly healthy robot scores 2/4:
    the superseded row has left ``fetch_live`` and the forgotten row has left the database. Both
    are real, correct outcomes of AC-4 and AC-5 — and scored as recall they read as a model that
    forgot half of what it was told. No mode grades both phases, and this is why.
    """
    db = await _store(tmp_path, facts=_ALL_FACTS)  # superseded + forgotten
    criteria = await _run(db, phase="recall")

    recall = _by_ac(criteria, "AC-2")[0]
    assert recall.passed is False
    assert "2/4" in recall.name, "the two mutated facts read as recall misses"

    memory_pi = _load_memory_pi()
    with pytest.raises(
        SystemExit
    ):  # there is no --mode that grades both; argparse refuses it
        memory_pi.build_parser().parse_args(
            ["--config", "config/sim.toml", "--mode", "all"]
        )


async def test_a_missing_fact_fails_recall_against_the_stated_denominator(
    tmp_path: Path,
) -> None:
    """A fact the model never stored is a **recall** miss, not an invisible one (AC-2).

    This is the settled denominator doing its job: 3 of 4 stated facts is 75%, under O2's 95%, even
    though every row that *does* exist recalls perfectly. An extraction miss cannot hide.
    """
    db = await _store(
        tmp_path, facts=_ALL_FACTS[:3], supersede=None, forget=None
    )  # "I run on Tuesdays" never stored
    criteria = await _run(db, phase="recall")

    recall = _by_ac(criteria, "AC-2")[0]
    assert recall.passed is False
    assert "3/4" in recall.name and "75%" in recall.name
    assert any("run" in row for row in recall.rows)

    # ...and the inventory line reports the same gap without grading it twice.
    inventory = _by_ac(criteria, "AC-1")[0]
    assert inventory.passed is None
    assert "3/4" in inventory.name


async def test_an_announced_write_fails_the_silence_criterion(tmp_path: Path) -> None:
    """§7.6 says the model stores facts silently. A narrated write is a defect the store cannot
    show — only the transcript can, which is why the harness reads `episodes`."""
    db = await _store(
        tmp_path,
        facts=_ALL_FACTS,
        transcript="Got it, I'll remember that you drink black coffee.",
        supersede=None,
        forget=None,
    )
    criteria = await _run(db, phase="recall")
    silence = next(c for c in _by_ac(criteria, "AC-1") if "silent" in c.name)
    assert silence.passed is False
    assert "i'll remember" in silence.detail


async def test_an_empty_transcript_cannot_pass_the_silence_criterion(
    tmp_path: Path,
) -> None:
    """**A gate that can pass on silence is not a gate.**

    With no episodes at all, the announcement search runs over an empty string and trivially finds
    nothing — which is not evidence of silence, it is absence of evidence. The harness must say so
    rather than bank the free PASS.
    """
    db = await _store(
        tmp_path, facts=_ALL_FACTS, transcript=None, supersede=None, forget=None
    )
    criteria = await _run(db, phase="recall")
    assert any(
        c.passed is False and "transcript available" in c.name
        for c in _by_ac(criteria, "AC-1")
    )


async def test_a_supersession_that_never_marked_the_old_row_fails(
    tmp_path: Path,
) -> None:
    """Two rows about coffee and tea, neither pointing at the other, is not supersession (AC-4) —
    it is contradiction, sitting in the store, waiting to be retrieved at random."""
    db = await _store(tmp_path, facts=_ALL_FACTS, supersede=None)
    now = int(time.time())
    await asyncio.to_thread(  # the new fact exists, but nothing was ever marked superseded
        _sql,
        db,
        "INSERT INTO facts(text, kind, importance, created_at, last_accessed_at) "
        f"VALUES ('I have switched to tea', 'other', 5, {now}, {now})",
    )
    criteria = await _run(db, phase="mutations")
    pointer = next(c for c in _by_ac(criteria, "AC-4") if "pointer" in c.name)
    assert pointer.passed is False


async def test_a_forget_that_leaves_the_fts_index_behind_fails(tmp_path: Path) -> None:
    """The one AC-5 check a schema `SELECT` cannot make.

    ``facts_fts`` is **external-content**, so a stale index entry whose content row is gone reads
    back as empty — query it directly and a leak looks clean. Here the row is deleted with the
    delete trigger dropped, exactly as a bad migration or a raw `DELETE` would leave it: the text is
    still searchable, and the harness has to notice via the search, not via the table.
    """
    db = await _store(tmp_path, facts=_ALL_FACTS, forget=None)
    await asyncio.to_thread(
        _sql,
        db,
        "DROP TRIGGER facts_fts_ad",  # the guard this test exists to prove
        "DELETE FROM facts WHERE text LIKE '%Biscuit%'",
    )

    criteria = await _run(db, phase="mutations")
    gone = next(c for c in _by_ac(criteria, "AC-5") if "gone from" in c.name)
    orphan = next(c for c in _by_ac(criteria, "AC-5") if "index entry" in c.name)
    assert gone.passed is True, "the row itself really was deleted"
    assert orphan.passed is False, (
        "the deleted fact is still searchable — a hard delete that leaves the index behind is not "
        "a hard delete, and this is the check that has to catch it"
    )


async def test_a_row_that_was_never_deleted_fails_forget(tmp_path: Path) -> None:
    """The blunt case: "forget that" that quietly did nothing (AC-5)."""
    db = await _store(tmp_path, facts=_ALL_FACTS, forget=None)
    criteria = await _run(db, phase="mutations")
    gone = next(c for c in _by_ac(criteria, "AC-5") if "gone from" in c.name)
    assert gone.passed is False
    assert "STILL PRESENT" in gone.detail


async def test_every_criterion_reports_even_when_an_earlier_one_fails(
    tmp_path: Path,
) -> None:
    """CLAUDE.md §7.1: a failing criterion must not hide a passing one.

    M5 shipped a reporter that returned on its first failure and buried a criterion that had passed.
    Here AC-2 fails on a missing fact while AC-3/AC-4/AC-5 still report — and the run is still a
    failure, so honesty and strictness are not traded against each other.
    """
    db = await _store(tmp_path, facts=_ALL_FACTS[:3], supersede=None, forget=None)
    criteria = await _run(db, phase="recall")

    assert {c.ac for c in criteria} == {"AC-1", "AC-2", "AC-3"}
    assert _by_ac(criteria, "AC-2")[0].passed is False
    assert all(c.passed is True for c in _by_ac(criteria, "AC-3")), (
        "AC-3 passed and must still say so — M5 shipped a reporter that returned on its first "
        "failure and buried a criterion that had passed"
    )
    assert _load_memory_pi()._report(criteria) == 1


def test_the_shipped_script_declares_twenty_facts_and_a_proper_noun_probe() -> None:
    """The committed corpus is part of the criterion, not a sample.

    AC-2's denominator is "the 20 facts stated", so a script that drifts to 19 or 21 silently
    changes the bar it is graded against. AC-3 requires a proper-noun query by name.
    """
    path = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "demos"
        / "m7_evidence"
        / "facts.json"
    )
    script = json.loads(path.read_text(encoding="utf-8"))

    assert len(script["facts"]) == 20
    assert len({f["key"] for f in script["facts"]}) == 20, (
        "duplicate keys would collapse the count"
    )
    assert {f["kind"] for f in script["facts"]} >= {
        "identity",
        "preference",
        "routine",
        "relationship",
        "event",
    }, "AC-1 asks for identity, preferences, routines, relationships and events"
    assert any(p.get("proper_noun") for p in script["semantic_probes"]), (
        "AC-3 requires one"
    )

    # The supersession and forget targets must name real facts, or those ACs cannot run at all.
    keys = {f["key"] for f in script["facts"]}
    assert script["supersession"]["supersedes"] in keys
    assert script["forget"]["target"] in keys
    assert all(p["expect"] in keys for p in script["semantic_probes"])


@pytest.mark.parametrize(
    "needles,text,expected",
    [
        (["coffee"], "I drink black coffee", True),
        (["coffee"], "I drink tea now", False),
        (["run", "tuesday"], "I run on Tuesdays", True),
        (["run", "tuesday"], "I run on Thursdays", False),  # both needles required
        (["maya"], "MY SISTER MAYA IS A DOCTOR", True),  # case-folded
    ],
)
def test_the_matcher_is_loose_on_phrasing_and_strict_on_content(
    needles: list[str], text: str, expected: bool
) -> None:
    """§7.6 stores the user's own words, so the matcher cannot demand an exact sentence — but a
    matcher loose enough to score "tea" as the coffee fact would make every other check meaningless.
    """
    assert _load_memory_pi()._matches(text, needles) is expected
