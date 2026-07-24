"""Contract suite for the ``FactRepository`` port (#117, SDS §8.3, §8.4, §8.6).

A port's contract runs against *every* adapter, real and fake, so the fake can never quietly
drift from the real thing (P6, SDS §3.9.2). The shared tier below is parametrized over
:class:`SqliteFactRepo` (a real file DB) **and** :class:`FakeFactRepository` (``":memory:"``)
and asserts the port invariants that must hold for either. Neither needs the Pi — SQLite runs
everywhere — so unlike the device HALs there is no ``hardware`` mark here: both legs run on CI.

Two further tiers exercise the *schema and runner* directly through ``connect``/``migrate``,
because their guarantees (the ``ON DELETE CASCADE``, the supersession ``CHECK``, the checksum
append-only rule) live below the port's vocabulary — they are the database's behaviour, and the
whole point of §8.4/§8.6 is that they are enforced by the DB, not merely avoided by the code.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest

from avid.adapters.clock import FakeClock
from avid.adapters.fact_repository import (
    FakeFactRepository,
    SqliteFactRepo,
    _row_to_fact,
)
from avid.adapters.sqlite import check_fts5, connect, migrate
from avid.core.ports import FactRepository
from avid.domain import Fact


def make_fact(**overrides: object) -> Fact:
    """A live relationship fact with sensible defaults; override any field per test."""
    base: dict[str, object] = {
        "id": 0,  # ignored on insert — the DB assigns the rowid
        "text": "Maya is my daughter",
        "kind": "relationship",
        "importance": 8,
        "confidence": 1.0,
        "created_at": 1_000,
        "last_accessed_at": 1_000,
        "access_count": 0,
    }
    base.update(overrides)
    return Fact(**base)  # type: ignore[arg-type]


# --- shared contract: every FactRepository adapter must satisfy it ----------


@pytest.fixture(params=["sqlite", "fake"])
async def repo(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> AsyncIterator[FactRepository]:
    clock = FakeClock()
    r: FactRepository
    if request.param == "sqlite":
        db = tmp_path_factory.mktemp("factrepo") / "robot.db"
        r = SqliteFactRepo(db_path=db, clock=clock)
    else:
        r = FakeFactRepository(clock=clock)
    try:
        yield r
    finally:
        await r.aclose()


async def test_adapter_satisfies_the_port(repo: FactRepository) -> None:
    assert isinstance(repo, FactRepository)


async def test_add_returns_id_and_get_round_trips(repo: FactRepository) -> None:
    corr = uuid4()
    fact = make_fact(
        confidence=0.5,
        access_count=3,
        derived_from=(11, 22),
        source_correlation_id=corr,
    )
    new_id = await repo.add(fact)
    assert isinstance(new_id, int) and new_id > 0

    got = await repo.get(new_id)
    assert got is not None
    # Every field round-trips; only the DB-assigned id differs from the input.
    assert got == Fact(
        id=new_id,
        text="Maya is my daughter",
        kind="relationship",
        importance=8,
        confidence=0.5,
        created_at=1_000,
        last_accessed_at=1_000,
        access_count=3,
        derived_from=(11, 22),
        source_correlation_id=corr,
    )


async def test_get_missing_returns_none(repo: FactRepository) -> None:
    assert await repo.get(9999) is None


async def test_defaults_round_trip(repo: FactRepository) -> None:
    """A fact carrying only the required fields comes back with the domain defaults intact."""
    new_id = await repo.add(make_fact())
    got = await repo.get(new_id)
    assert got is not None
    assert got.confidence == 1.0
    assert got.access_count == 0
    assert got.derived_from == ()
    assert got.superseded_by is None
    assert got.superseded_at is None
    assert got.source_correlation_id is None


async def test_fetch_live_excludes_superseded_and_orders_by_recency(
    repo: FactRepository,
) -> None:
    old = await repo.add(make_fact(text="drinks coffee", last_accessed_at=100))
    mid = await repo.add(make_fact(text="drinks tea now", last_accessed_at=300))
    new = await repo.add(make_fact(text="lives in Beirut", last_accessed_at=500))
    # The tea fact supersedes the coffee fact.
    await repo.mark_superseded(old, mid, at=350)

    live = await repo.fetch_live()
    ids = [f.id for f in live]
    assert old not in ids  # superseded → excluded
    assert ids == [new, mid]  # most-recently-accessed first (idx_facts_live order)


async def test_mark_superseded_sets_both_pointer_columns(repo: FactRepository) -> None:
    a = await repo.add(make_fact(text="old"))
    b = await repo.add(make_fact(text="new"))
    await repo.mark_superseded(a, b, at=1_234)

    got = await repo.get(a)
    assert got is not None
    assert got.superseded_by == b
    assert got.superseded_at == 1_234


async def test_delete_removes_the_fact(repo: FactRepository) -> None:
    fid = await repo.add(make_fact())
    await repo.delete(fid)
    assert await repo.get(fid) is None


async def test_load_embeddings_returns_only_facts_with_a_blob(
    repo: FactRepository,
) -> None:
    blob = bytes(range(16))
    with_emb = await repo.add(make_fact(text="has a vector"), embedding=blob)
    await repo.add(make_fact(text="no vector yet"))  # embedding None → excluded

    loaded = await repo.load_embeddings()
    assert loaded == [(with_emb, blob)]


async def test_load_embeddings_excludes_superseded(repo: FactRepository) -> None:
    blob = bytes(range(8))
    a = await repo.add(make_fact(text="superseded but embedded"), embedding=blob)
    b = await repo.add(make_fact(text="replacement"))
    await repo.mark_superseded(a, b, at=42)
    assert await repo.load_embeddings() == []


async def test_aclose_is_idempotent(repo: FactRepository) -> None:
    await repo.add(make_fact())
    await repo.aclose()
    await repo.aclose()  # second call must not raise


# --- SqliteFactRepo-specific: the db_path / directory contract (AC-8) --------


async def test_sqlite_repo_creates_a_missing_parent_directory(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A db_path under a not-yet-existing directory must not crash boot with a bare
    OperationalError — the adapter makes the parent (AC-8)."""
    root = tmp_path_factory.mktemp("mkdir")
    db = root / "does" / "not" / "exist" / "robot.db"
    repo = SqliteFactRepo(db_path=db, clock=FakeClock())
    try:
        await repo.add(make_fact())  # first use opens the DB, creating the dirs
        assert db.exists()
    finally:
        await repo.aclose()


async def test_a_few_thousand_rows_stay_off_the_loop(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """AC-5, P8: a few-thousand-row store still touches SQLite only on the writer thread.
    Under ``PYTHONASYNCIODEBUG=1`` (how CI runs the async-debug gate) any query that blocked
    the loop >50 ms would fail the run; the bulk insert + the full-table scan below are the
    load that would trip it if the offload regressed."""
    db = tmp_path_factory.mktemp("load") / "robot.db"
    repo = SqliteFactRepo(db_path=db, clock=FakeClock())
    try:
        for i in range(3_000):
            await repo.add(make_fact(text=f"fact {i}", last_accessed_at=i))
        live = await repo.fetch_live()  # one scan over 3,000 rows, on the writer thread
        assert len(live) == 3_000
        assert live[0].last_accessed_at == 2_999  # ordered most-recent-first
    finally:
        await repo.aclose()


# --- schema tier: the PRAGMA and CHECK are the database's, not the code's -----


def test_foreign_keys_on_cascades_a_delete(tmp_path: Path) -> None:
    """With foreign_keys ON (§8.4), deleting a fact cascades to its routine and trigger —
    the load-bearing half of forget() (§7.10)."""
    conn = connect(tmp_path / "cascade.db")
    try:
        migrate(conn, now=0)
        conn.execute(
            "INSERT INTO facts (text, kind, importance, created_at, last_accessed_at) "
            "VALUES ('coffee at 7', 'routine', 5, 0, 0)"
        )
        fid = conn.execute("SELECT id FROM facts").fetchone()["id"]
        conn.execute(
            "INSERT INTO routines (fact_id, rrule, local_time, timezone) "
            "VALUES (?, 'FREQ=DAILY', '07:00', 'Asia/Beirut')",
            (fid,),
        )
        conn.execute(
            "INSERT INTO triggers (fact_id, kind) VALUES (?, 'schedule')", (fid,)
        )
        conn.commit()

        conn.execute("DELETE FROM facts WHERE id = ?", (fid,))
        conn.commit()

        assert conn.execute("SELECT COUNT(*) c FROM routines").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM triggers").fetchone()["c"] == 0
    finally:
        conn.close()


def test_foreign_keys_off_would_orphan_children(tmp_path: Path) -> None:
    """Proving the PRAGMA is load-bearing: with foreign_keys OFF the same delete orphans the
    child rows. This is the trap §8.4 warns about — a privacy bug, not a tidiness bug."""
    path = tmp_path / "orphan.db"
    conn = connect(path)
    try:
        migrate(conn, now=0)
    finally:
        conn.close()

    raw = sqlite3.connect(path)  # a fresh connection WITHOUT connect()'s PRAGMAs
    raw.execute("PRAGMA foreign_keys = OFF")
    try:
        raw.execute(
            "INSERT INTO facts (text, kind, importance, created_at, last_accessed_at) "
            "VALUES ('x', 'routine', 5, 0, 0)"
        )
        fid = raw.execute("SELECT id FROM facts").fetchone()[0]
        raw.execute(
            "INSERT INTO routines (fact_id, rrule, local_time, timezone) "
            "VALUES (?, 'FREQ=DAILY', '07:00', 'Asia/Beirut')",
            (fid,),
        )
        raw.commit()
        raw.execute("DELETE FROM facts WHERE id = ?", (fid,))
        raw.commit()
        # The routine is orphaned — exactly what foreign_keys = ON prevents.
        assert raw.execute("SELECT COUNT(*) FROM routines").fetchone()[0] == 1
    finally:
        raw.close()


def test_half_supersession_is_rejected_by_the_check(tmp_path: Path) -> None:
    """superseded_by and superseded_at must be set together — a direct write of one without
    the other is rejected by the DB (AC-7), not merely avoided by mark_superseded()."""
    conn = connect(tmp_path / "check.db")
    try:
        migrate(conn, now=0)
        conn.execute(
            "INSERT INTO facts (text, kind, importance, created_at, last_accessed_at) "
            "VALUES ('x', 'other', 5, 0, 0)"
        )
        fid = conn.execute("SELECT id FROM facts").fetchone()["id"]
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE facts SET superseded_by = ? WHERE id = ?", (fid, fid))
            conn.commit()
    finally:
        conn.close()


# --- runner tier: the migration bookkeeping (AC-2) ---------------------------


def test_migrate_applies_0001_and_records_its_checksum(tmp_path: Path) -> None:
    conn = connect(tmp_path / "m.db")
    try:
        migrate(conn, now=123)
        rows = conn.execute(
            "SELECT version, applied_at, checksum FROM schema_migrations"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["version"] == 1
        assert rows[0]["applied_at"] == 123
        assert len(rows[0]["checksum"]) == 64  # a sha256 hex digest
        # The schema is really there.
        assert conn.execute("SELECT COUNT(*) c FROM facts").fetchone()["c"] == 0
    finally:
        conn.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "idem.db")
    try:
        migrate(conn, now=1)
        migrate(
            conn, now=2
        )  # second run: version already applied, checksum matches → no-op
        count = conn.execute("SELECT COUNT(*) c FROM schema_migrations").fetchone()["c"]
        assert count == 1
    finally:
        conn.close()


def test_migrate_rejects_a_tampered_applied_migration(tmp_path: Path) -> None:
    """Editing an applied migration must fail loudly (§8.6, the append-only rule)."""
    conn = connect(tmp_path / "tamper.db")
    try:
        migrate(conn, now=1)
        conn.execute(
            "UPDATE schema_migrations SET checksum = 'deadbeef' WHERE version = 1"
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="append-only"):
            migrate(conn, now=2)
    finally:
        conn.close()


def test_check_fts5_wraps_a_missing_build_option() -> None:
    """If FTS5 is absent, the probe raises a clear ENABLE_FTS5 error rather than a cryptic
    OperationalError bubbling out of a CREATE VIRTUAL TABLE at boot."""

    class _NoFts5:
        def execute(self, _sql: str) -> object:
            raise sqlite3.OperationalError("no such module: fts5")

    with pytest.raises(RuntimeError, match="ENABLE_FTS5"):
        check_fts5(_NoFts5())  # type: ignore[arg-type]


def test_check_fts5_passes_on_a_real_connection(tmp_path: Path) -> None:
    conn = connect(tmp_path / "fts.db")
    try:
        check_fts5(conn)  # dev SQLite has FTS5 — must not raise
    finally:
        conn.close()


def test_row_to_fact_maps_all_columns(tmp_path: Path) -> None:
    """The row mapper is adapter-internal but its correctness underpins every read; pin it
    directly against a known row so a column reorder cannot pass silently."""
    conn = connect(tmp_path / "map.db")
    try:
        migrate(conn, now=0)
        corr = uuid4()
        conn.execute(
            "INSERT INTO facts (text, kind, importance, confidence, created_at, "
            "last_accessed_at, access_count, derived_from, source_correlation_id) "
            "VALUES ('t', 'event', 9, 0.25, 10, 20, 4, '[1, 2, 3]', ?)",
            (str(corr),),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, text, kind, importance, confidence, created_at, last_accessed_at, "
            "access_count, superseded_by, superseded_at, derived_from, source_correlation_id "
            "FROM facts"
        ).fetchone()
        fact = _row_to_fact(row)
        assert fact.text == "t"
        assert fact.kind == "event"
        assert fact.importance == 9
        assert fact.confidence == 0.25
        assert fact.created_at == 10
        assert fact.last_accessed_at == 20
        assert fact.access_count == 4
        assert fact.derived_from == (1, 2, 3)
        assert fact.source_correlation_id == corr
    finally:
        conn.close()
