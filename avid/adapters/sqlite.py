"""SQLite connection + migration plumbing shared by every on-device repository (#117).

The durable store's foundation (SDS §8). Three concerns live here, none of them a port —
they are the machinery every repo adapter (`SqliteFactRepo` now; episodes/triggers in M10)
opens its connection through:

* :func:`connect` — a connection with the eight §8.4 PRAGMAs applied. **Per connection**,
  because SQLite defaults ``foreign_keys`` *off*: without it every ``ON DELETE CASCADE`` in
  the schema is decorative and ``forget()`` silently orphans embeddings and triggers — a
  privacy bug, not a tidiness bug (§8.4).
* :func:`check_fts5` — a loud, early assertion that this SQLite build has FTS5, so a missing
  compile option surfaces as a clear error at migrate time rather than a cryptic
  ``CREATE VIRTUAL TABLE`` crash three tables into boot on the Pi (§8.3 note).
* :func:`migrate` — the ~50-line runner (§8.6): numbered SQL files, applied in order, one
  transaction each, **checksummed**. Editing an already-applied migration fails loudly rather
  than letting a dev DB silently diverge from the Pi's. No Alembic (§8.6 says why at length).

Blocking, synchronous ``sqlite3`` (P8): nothing here is ``async``. Callers run these off the
event loop on a dedicated writer thread — that is the adapter's job (§3.8.2), not this
module's.
"""

from __future__ import annotations

import hashlib
import sqlite3
from importlib.resources import files
from pathlib import Path

# The §8.4 connection PRAGMAs, in order. Every one is an SD-card decision (R-05), not a
# performance tweak: WAL + NORMAL is the durable-enough sweet spot, temp_store = MEMORY keeps
# a sort from silently writing to the card, and foreign_keys = ON is the load-bearing one —
# SQLite defaults it off, so the cascades are decorative without it (§8.4).
_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode   = WAL",
    "PRAGMA synchronous    = NORMAL",
    "PRAGMA foreign_keys   = ON",
    "PRAGMA busy_timeout   = 5000",
    "PRAGMA temp_store     = MEMORY",
    "PRAGMA cache_size     = -16000",
    "PRAGMA wal_autocheckpoint = 1000",
    "PRAGMA mmap_size      = 268435456",
)

# The in-package migrations directory (decision: package data, not a repo-root dir, so it ships
# in the wheel — `packages = ["avid"]`). Resolved through importlib.resources so it is found
# identically from the source tree and from an installed wheel.
_MIGRATIONS = files("avid.adapters").joinpath("migrations")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection to ``db_path`` with the §8.4 PRAGMAs and ``Row`` access.

    Creates the parent directory first (AC-8): a missing ``/var/lib/robot`` must not surface
    as a bare ``OperationalError`` at boot. ``":memory:"`` has no parent to make, so it is
    skipped — that is the path the in-memory fake opens.
    """
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    for pragma in _PRAGMAS:
        conn.execute(pragma)
    return conn


def check_fts5(conn: sqlite3.Connection) -> None:
    """Assert this SQLite build has FTS5, raising a clear error if not (§8.3 note).

    FTS5 is present in dev Python and Debian bookworm's libsqlite3, but *asserting* it beats
    letting the ``facts_fts`` ``CREATE VIRTUAL TABLE`` fail as a cryptic boot crash on a Pi
    whose SQLite was built without ``ENABLE_FTS5``. The probe builds and drops a throwaway
    table in the per-connection ``temp`` database, touching nothing durable.
    """
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.__fts5_probe")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "SQLite was built without FTS5 (ENABLE_FTS5); the memory schema's facts_fts "
            "table cannot be created. On the Pi, ensure libsqlite3 has FTS5 compiled in. "
            f"Underlying error: {exc}"
        ) from exc


def _applied(conn: sqlite3.Connection) -> dict[int, str]:
    """The ``{version: checksum}`` already recorded, or ``{}`` before the first migration.

    ``schema_migrations`` is itself created *by* ``0001_initial.sql``, so on a virgin DB the
    table does not yet exist — that is not an error, it is the bootstrap, and it reads as an
    empty applied set.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if exists is None:
        return {}
    return {
        int(row["version"]): str(row["checksum"])
        for row in conn.execute("SELECT version, checksum FROM schema_migrations")
    }


def migrate(conn: sqlite3.Connection, *, now: int) -> None:
    """Apply every unapplied migration in order, verifying applied ones are unchanged (§8.6).

    The append-only guarantee is mechanical: for each already-applied version the file's
    sha256 must still match what ``schema_migrations`` recorded — a mismatch means someone
    edited a merged migration, and boot fails **loudly** here rather than the dev DB quietly
    diverging from the Pi's. Unapplied versions run in ascending order, each as **one
    transaction** (the embedded ``BEGIN … COMMIT``), so a failing migration leaves no partial
    schema. ``now`` (epoch seconds, §8.2) stamps ``applied_at``; it is injected, never read.
    """
    check_fts5(conn)
    applied = _applied(conn)
    for entry in sorted(_MIGRATIONS.iterdir(), key=lambda p: p.name):
        if not entry.name.endswith(".sql"):
            continue
        version = int(entry.name.split("_", 1)[0])
        sql = entry.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        if version in applied:
            if applied[version] != checksum:
                raise RuntimeError(
                    f"migration {entry.name} has changed since it was applied "
                    f"(recorded checksum {applied[version][:12]}…, file is "
                    f"{checksum[:12]}…). Migrations are append-only (§8.6); revert the edit "
                    "and add a new migration instead."
                )
            continue
        # One transaction per migration. executescript() commits any pending transaction and
        # ignores isolation_level, so the DDL + the bookkeeping INSERT are wrapped in an
        # explicit BEGIN…COMMIT inside the script. The inlined scalars are a sha256 hex digest
        # and two ints — not user input, so this is bookkeeping, not an injection surface.
        conn.executescript(
            "BEGIN;\n"
            f"{sql}\n"
            "INSERT INTO schema_migrations(version, applied_at, checksum) "
            f"VALUES ({version}, {now}, '{checksum}');\n"
            "COMMIT;"
        )
