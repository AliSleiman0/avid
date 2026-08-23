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
from avid.core.ports import Embedder
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
    embedder: Embedder | None = None,
) -> Path:
    """Build a real store: insert ``facts``, optionally supersede a pair and delete a row.

    Returns the db path. Everything goes through :class:`SqliteFactRepo`, so the FTS5 triggers and
    the schema constraints are the real ones — which is the only way the AC-5 orphan test below can
    mean anything.
    """
    db_path = tmp_path / "m7.db"
    clock = SystemClock()
    repo = SqliteFactRepo(db_path=db_path, clock=clock)
    embedder = embedder or FakeEmbedder(dimensions=384)
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

        # The snapshot the recall phase would write, taken here because that is when it is taken
        # in production: while the store is still intact, before the supersession and forget turns.
        # Building it afterwards is exactly the mistake #258 is about.
        snap = {
            d["key"]: next(
                (i for t, i in ids.items() if all(n in t.lower() for n in d["match"])),
                None,
            )
            for d in _SCRIPT["facts"]
        }
        (db_path.parent / "snapshot.json").write_text(
            json.dumps({"snapshot": snap}), encoding="utf-8"
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
    db_path: Path,
    script: dict[str, Any] | None = None,
    *,
    phase: str = "recall",
    snapshot_before: dict[str, int | None] | None = "auto",  # type: ignore[assignment]
    embedder: Embedder | None = None,
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
            embedder=embedder or FakeEmbedder(dimensions=384),
            bus=bus,
            clock=clock,
        )
        await retriever.rebuild()
        spec = script if script is not None else _SCRIPT
        if snapshot_before == "auto":
            # What _store recorded before it mutated anything — the production ordering. Tests
            # about a missing precondition pass None explicitly instead.
            snap_file = db_path.parent / "snapshot.json"
            snapshot_before = (
                json.loads(snap_file.read_text(encoding="utf-8"))["snapshot"]
                if snap_file.is_file()
                else None
            )
        if phase == "recall":
            criteria = list(memory_pi._inventory(spec, facts, db_path))
            criteria += await memory_pi._recall(spec, facts, retriever)
            criteria += await memory_pi._semantic(spec, facts, retriever)
        else:
            criteria = list(await memory_pi._supersede(spec, facts, retriever, db_path))
            criteria += await memory_pi._forget(
                spec, repo, retriever, db_path, snapshot_before
            )
        return criteria
    finally:
        await repo.aclose()
        await bus.stop()


def _failed(criteria: list[Any]) -> list[str]:
    return [f"{c.ac} {c.name}" for c in criteria if c.verdict == "fail"]


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
    assert recall.verdict == "fail"
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
    assert recall.verdict == "fail"
    assert "3/4" in recall.name and "75%" in recall.name
    assert any("run" in row for row in recall.rows)

    # ...and the inventory line reports the same gap without grading it twice.
    inventory = _by_ac(criteria, "AC-1")[0]
    assert inventory.verdict == "recorded"
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
    assert silence.verdict == "fail"
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
        c.verdict == "fail" and "transcript available" in c.name
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
    assert pointer.verdict == "fail"


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
    assert gone.verdict == "pass", "the row itself really was deleted"
    assert orphan.verdict == "fail", (
        "the deleted fact is still searchable — a hard delete that leaves the index behind is not "
        "a hard delete, and this is the check that has to catch it"
    )


async def test_a_row_that_was_never_deleted_fails_forget(tmp_path: Path) -> None:
    """The blunt case: "forget that" that quietly did nothing (AC-5)."""
    db = await _store(tmp_path, facts=_ALL_FACTS, forget=None)
    criteria = await _run(db, phase="mutations")
    gone = next(c for c in _by_ac(criteria, "AC-5") if "gone from" in c.name)
    assert gone.verdict == "fail"
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
    assert _by_ac(criteria, "AC-2")[0].verdict == "fail"
    assert all(c.verdict == "pass" for c in _by_ac(criteria, "AC-3")), (
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


def _config_file(tmp_path: Path, db_path: Path) -> Path:
    """A real TOML pointing at the store — so the harness's own `main` does the config loading."""
    text = Path("config/sim.toml").read_text(encoding="utf-8")
    text = text.replace(
        'db_path                = ".artifacts/sim.db"',
        f'db_path = "{db_path.as_posix()}"',
    )
    out = tmp_path / "gate.toml"
    out.write_text(text, encoding="utf-8")
    return out


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("recall", {"AC-1", "AC-2", "AC-3"}), ("mutations", {"AC-4", "AC-5"})],
)
async def test_each_phase_grades_only_its_own_criteria(
    tmp_path: Path, mode: str, expected: set[str]
) -> None:
    """The mode → criteria dispatch, driven through the harness's real `main` and argv.

    Every other test here calls the criterion functions directly with an explicit phase, which
    means none of them can see the dispatch. That gap is not hypothetical: collapsing the two
    phases back into one — the exact defect this design exists to prevent — left all of them
    green. This test is the one that goes red, so the phases are enforced by more than a comment.
    """
    db = await _store(tmp_path, facts=_ALL_FACTS)
    config = _config_file(tmp_path, db)
    out = tmp_path / f"{mode}.json"

    memory_pi = _load_memory_pi()
    await asyncio.to_thread(
        memory_pi.main,
        [
            "--config",
            str(config),
            "--script",
            str(_script_file(tmp_path)),
            "--mode",
            mode,
            "--json",
            str(out),
        ],
    )

    graded = {c["ac"] for c in json.loads(out.read_text(encoding="utf-8"))["criteria"]}
    assert graded == expected, f"--mode {mode} graded {graded}, not {expected}"


def _script_file(tmp_path: Path) -> Path:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(_SCRIPT), encoding="utf-8")
    return path


# --- #258: AC-5 cannot report a vacuous PASS ---------------------------------------------------


async def test_a_fact_that_was_never_stored_is_inconclusive_not_passed(
    tmp_path: Path,
) -> None:
    """This issue, as a test (#258 AC-4).

    On the first M7 gate attempt the declared `dog` fact was never spoken, so it was never stored —
    and the harness reported **all three AC-5 criteria as PASS**, because "the row is gone" is
    trivially true of a row that never existed. That is the *gate that can pass on silence*
    failure, in the harness written to prevent it.

    It is not a failure either: the robot was never asked to forget anything. It is a precondition
    the run did not meet, and the only honest verdict is INCONCLUSIVE.
    """
    # Everything except the forget target, so the store is healthy and only the precondition is
    # absent — the neuter must be the one thing under test.
    without_dog = tuple(t for t in _ALL_FACTS if "Biscuit" not in t)
    db = await _store(tmp_path, facts=without_dog, supersede=None, forget=None)

    criteria = await _run(db, phase="mutations")
    ac5 = _by_ac(criteria, "AC-5")

    assert ac5, "AC-5 must still report, not vanish"
    assert not any(c.verdict == "pass" for c in ac5), (
        "a fact that was never stored cannot yield a PASS for having been deleted — this is the "
        "vacuous pass #258 was filed for"
    )
    assert any(c.verdict == "inconclusive" for c in ac5)
    assert "never stored" in " ".join(c.detail for c in ac5)


async def test_an_inconclusive_run_does_not_exit_as_success(tmp_path: Path) -> None:
    """Inconclusive must not read as a pass to a script, to CI, or to a tired human at midnight.

    The verdict is only worth having if the exit code carries it: a run that proved nothing and
    returned 0 is indistinguishable from a run that proved everything.
    """
    # Supersession DID happen (so AC-4 passes) and the forget target was simply never stored, so
    # the only thing wrong with this run is the missing precondition.
    without_dog = tuple(t for t in _ALL_FACTS if "Biscuit" not in t)
    db = await _store(tmp_path, facts=without_dog, forget=None)

    criteria = await _run(db, phase="mutations")
    assert _failed(criteria) == [], "nothing here should be a FAILURE"
    assert _load_memory_pi()._report(criteria) == 1, (
        "no failures, but the run still must not exit 0 — it did not establish what it claims"
    )


async def test_without_a_snapshot_ac5_is_inconclusive_rather_than_guessing(
    tmp_path: Path,
) -> None:
    """The mutation phase refuses to grade AC-5 on a store alone (#258 AC-1/AC-2).

    By the time it runs, a forgotten fact and a fact that was never stored are both simply absent.
    Without the recall phase's snapshot there is no evidence either way, and the harness says so
    instead of picking the flattering reading.
    """
    db = await _store(tmp_path, facts=_ALL_FACTS)  # a genuinely correct forget happened
    criteria = await _run(db, phase="mutations", snapshot_before=None)

    ac5 = _by_ac(criteria, "AC-5")
    assert [c.verdict for c in ac5] == ["inconclusive"]
    assert "no recall-phase snapshot" in ac5[0].detail


async def test_ac5_matches_the_row_by_id_not_by_text(tmp_path: Path) -> None:
    """#258 AC-5: identity, not wording.

    A fact is deleted and something similar is written afterwards — a plausible sequence, since the
    user may simply say it again. Matching on text would find the *new* row and report the old one
    as still present. The snapshot records ids, so the check follows the row that actually went.
    """
    db = await _store(
        tmp_path, facts=_ALL_FACTS
    )  # the Biscuit row is deleted by _store
    await asyncio.to_thread(
        _sql,
        db,
        "INSERT INTO facts(text, kind, importance, created_at, last_accessed_at) "
        f"VALUES ('The dog is called Biscuit', 'other', 5, {int(time.time())}, {int(time.time())})",
    )

    criteria = await _run(db, phase="mutations")
    gone = next(c for c in _by_ac(criteria, "AC-5") if "gone from" in c.name)
    assert gone.verdict == "pass", (
        "the forgotten ROW is gone; a later row with the same wording is a different fact and "
        "must not be mistaken for it"
    )


# --- #264 AC-2/AC-3: the proper-noun miss, against the embedder that actually produced it --------


_REPO_ROOT = Path(__file__).resolve().parents[2]
_MODEL_DIR = _REPO_ROOT / "models"
_MODEL = _MODEL_DIR / "all-MiniLM-L6-v2.onnx"
_TOKENIZER = _MODEL_DIR / "tokenizer.json"

_HOW_TO_RUN = (
    "needs the real MiniLM blobs and onnxruntime. Run:\n"
    "  python tools/fetch_minilm.py --dest ./models\n"
    "  uv run --frozen --with onnxruntime --with tokenizers pytest -m real_embedder"
)


def _real_embedder() -> Embedder:
    """`LocalMiniLmEmbedder` over the locally-fetched blobs, or skip saying exactly how to get them.

    ⚠️ The skip reason names the two commands verbatim on purpose. A test nobody can work out how
    to run is a test nobody runs, and this one is deselected by default — so the reason line is the
    only documentation it has.
    """
    if not (_MODEL.is_file() and _TOKENIZER.is_file()):
        pytest.skip(_HOW_TO_RUN)
    try:
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError:
        pytest.skip(_HOW_TO_RUN)
    from avid.adapters.embedder import LocalMiniLmEmbedder

    return LocalMiniLmEmbedder(model_path=_MODEL, tokenizer_path=_TOKENIZER)


def _full_corpus() -> tuple[dict[str, Any], list[str]]:
    """The shipped 20-fact M7 corpus, not a miniature.

    ⚠️ The crowding is part of the defect. The original miss was against **18 live facts**, and
    this issue's own suspicion was *"crowded out by a larger fact set"* — a four-fact store would
    remove the very condition under test while looking like the same experiment.
    """
    spec = json.loads(
        (_REPO_ROOT / "docs" / "demos" / "m7_evidence" / "facts.json").read_text(
            encoding="utf-8"
        )
    )
    return spec, [fact["say"] for fact in spec["facts"]]


@pytest.mark.real_embedder
async def test_the_two_probes_that_failed_the_m7_gate_hit_against_a_real_minilm_store(
    tmp_path: Path,
) -> None:
    """#264's AC-2 and AC-3, judged on the two probes the issue actually names.

    Both failed on the 2026-08-14 M7 gate run, from two different angles against the same fact:
    `"what is the neighbour's dog called?"` (AC-2's recall probe) and `"tell me about Biscuit"`
    (AC-3's proper-noun probe — the branch §7.7 built FTS5 for, *"where a vector alone fails"*).

    ⚠️ **This cannot be tested with `FakeEmbedder`, which is why the issue sat open for nine days.**
    The fake is bag-of-words over sha256-seeded token vectors, so "Biscuit" gets a clean,
    well-separated vector and the query retrieves it on the vector branch alone — the miss is
    *structurally* unreproducible there. `2777b1a` said exactly that in its own commit message
    rather than ticking AC-2 on a green that meant nothing.

    ⚠️ **Scoped to those two probes, deliberately.** The full recall phase scores **18/20 = 90%**
    against this store, under O2's 19/20 bar — but the two misses are `guitar` and `marathon`,
    neither of them this issue's, and both pure semantic inference with no lexical anchor for FTS5
    to grip. Asserting the whole phase here would make #264 hostage to an unrelated defect; that
    one is **#447**. Asserting *these* probes is what #264 asked for.
    """
    embedder = _real_embedder()
    spec, facts = _full_corpus()
    db = await _store(
        tmp_path, facts=facts, supersede=None, forget=None, embedder=embedder
    )
    criteria = await _run(db, script=spec, phase="recall", embedder=embedder)

    # AC-3's three semantic probes, including both proper-noun cases, are graded as their own
    # criteria by the harness — so a miss on any of them is a named failure rather than a number.
    semantic = [c for c in criteria if c.ac == "AC-3"]
    assert semantic, (
        "the harness reported no AC-3 criteria — this test is grading nothing"
    )
    assert [c.name for c in semantic if c.verdict == "fail"] == [], (
        "a semantic probe missed against a real-MiniLM store"
    )

    # AC-2's own probe for the same fact, checked directly: the aggregate rate is #447's problem,
    # this one row is #264's.
    memory_pi = _load_memory_pi()
    live = list(await SqliteFactRepo(db_path=db, clock=SystemClock()).fetch_live())
    dog = next(d for d in spec["facts"] if d["key"] == "dog")
    retrieved, _ = await _probe_one(db, spec, dog["probe"], dog["match"], embedder)
    assert retrieved, (
        f"{dog['probe']!r} still misses {dog['say']!r} — #264's AC-2 probe"
    )
    assert memory_pi is not None and live  # the store really was built


@pytest.mark.real_embedder
async def test_the_real_embedder_ranks_the_proper_noun_first_with_or_without_the_keyword_term(
    tmp_path: Path,
) -> None:
    """⚠️ **The neuter that did not go the way it was written, and the finding is worth more.**

    This was written to prove #375's keyword term (δ) is what retrieves "Biscuit" — set δ to zero,
    watch the probe fail, conclude the fix bites. It does not fail. Measured over the 20-fact
    corpus with the real MiniLM:

    ========================================  =========  =========
    probe                                     δ = 1.0    δ = 0.0
    ========================================  =========  =========
    ``tell me about Biscuit``                 rank **1**  rank **1**
    ``what is the neighbour's dog called?``   rank **1**  rank **1**
    ========================================  =========  =========

    δ reshuffles the *tail* of the top-5 and never moves the target. So **the vector branch alone
    retrieves this fact**, and §7.7's premise — *"proper nouns embed to mush"* — does not hold for
    this fact and this model.

    What follows, stated carefully:

    * #264's two probes **pass** against a real-MiniLM store. That is what AC-2/AC-3 ask.
    * But the original miss is **not reproduced** in this configuration, by any setting of δ. So
      this suite cannot show *what* fixed it, and `2777b1a`'s causal claim is unsupported **for
      this probe**. (#375 remains right that the FTS5 branch could not reach the score at all
      before it — that is a separate, proven claim with its own δ tests in
      ``tests/domain/test_memory.py``.)
    * The leading hypothesis for 2026-08-14 is an **extraction** miss, not a retrieval one:
      ``memory_pi._probe`` returns False when the fact was never stored — *"never stored ⇒ cannot
      be recalled ⇒ a miss, per AC-2"* — so an absent fact is **indistinguishable** from a
      retrieval failure in that harness, and it would explain both probes failing on the same fact
      from two angles while the corpus's other probes passed. ⚠️ Unprovable now: that run's
      ``recall_result.json`` was never committed, only ``facts.json`` was.

    This test therefore asserts the measured truth rather than the expected one. It is a live
    detector: if a change to the embedder or the ranking ever makes δ load-bearing here, this flips
    and someone re-reads the paragraph above instead of inheriting a comfortable assumption.
    """
    embedder = _real_embedder()
    spec, facts = _full_corpus()
    db = await _store(
        tmp_path, facts=facts, supersede=None, forget=None, embedder=embedder
    )

    from avid.adapters import HybridRetriever
    from avid.domain.memory import ScoreWeights

    config = _config(db)
    clock = SystemClock()
    bus = AsyncioEventBus(clock=clock)
    await bus.start()
    repo = SqliteFactRepo(db_path=db, clock=clock)
    try:
        live = list(await repo.fetch_live())
        expected = next(f for f in live if "biscuit" in f.text.lower()).id

        async def _rank(query: str, keyword: float) -> int | None:
            retriever = HybridRetriever(
                repo=repo,
                embedder=embedder,
                bus=bus,
                clock=clock,
                weights=ScoreWeights(keyword=keyword),
                top_k=config.memory.top_k,
                half_life_days=config.memory.recency_half_life_days,
            )
            await retriever.rebuild()
            ids = list(await retriever.retrieve(query))
            return ids.index(expected) + 1 if expected in ids else None

        for query in ("tell me about Biscuit", "what is the neighbour's dog called?"):
            with_delta = await _rank(query, 1.0)
            without = await _rank(query, 0.0)
            assert with_delta == 1, f"{query!r} no longer ranks the fact first (δ=1)"
            assert without == 1, (
                f"{query!r} now DEPENDS on δ (rank {without} without it). That is a change from "
                "the 2026-08-23 measurement, where the vector branch carried this alone — re-read "
                "this test's docstring before treating it as a pass."
            )
    finally:
        await repo.aclose()
        await bus.stop()


async def _probe_one(
    db_path: Path,
    spec: dict[str, Any],
    query: str,
    needles: list[str],
    embedder: Embedder,
) -> tuple[bool, float]:
    """Run ONE probe through the production retriever and say whether it reached the fact.

    Uses `memory_pi._probe`, the same function the gate grades with, so this cannot drift from the
    harness by paraphrasing it.
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
            config, repo=repo, embedder=embedder, bus=bus, clock=clock
        )
        await retriever.rebuild()
        return await memory_pi._probe(retriever, query, facts, needles)
    finally:
        await repo.aclose()
        await bus.stop()
