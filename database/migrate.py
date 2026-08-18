#!/usr/bin/env python3
"""Migration runner for the Agent OS central store (SQL Server ``agent_os_memory``).

Applies numbered ``database/migrations/NNN_description.sql`` files to the
central database in order, transactionally, with a checksum recorded per
applied migration so drift (a migration file edited after it was applied) is
detected rather than silently re-applied or ignored.

Connection
----------
The connection string lives ONLY in the ``AGENT_OS_CENTRAL_DB_CONNECTION_STRING``
environment variable (see ``.env``, gitignored). This module never prints,
logs, or otherwise echoes it — only :func:`load_connection_string` reads it,
and every error path here reports the env var NAME, never its value. Mirrors
the ``.env`` + bare ``load_dotenv()`` pattern in
``mcp/db_tools/build_db_graph.py``: the bare call walks upward from *this
file's* location, which is inside the repo, so it reliably finds the repo-root
``.env`` regardless of the caller's current working directory.

DATETIMEOFFSET gotcha
----------------------
pyodbc does not natively decode SQL Server's ``DATETIMEOFFSET`` wire type (ODBC
SQL type ``-155`` / ``SQL_SS_TIMESTAMPOFFSET``): fetching so much as one such
column without an output converter raises ``pyodbc.ProgrammingError: ODBC SQL
type -155 is not yet supported`` (verified live against pyodbc 5.3.0 + ODBC
Driver 17 for SQL Server against this exact database). Every table this card
creates uses ``DATETIMEOFFSET`` for its UTC-aware timestamps (including the
runner's own ``ops.schema_migrations.applied_at``), so :func:`connect` always
registers :func:`_decode_datetimeoffset` before returning the connection.

Non-transactional migrations
-----------------------------
Every migration is applied inside one transaction by default (commit on
success, rollback on any batch failure — see :func:`apply_migration`). Some
SQL Server DDL (``CREATE FULLTEXT CATALOG``/``CREATE FULLTEXT INDEX``,
``CREATE DATABASE``, ``ALTER DATABASE ... SET``, backup/restore, ...) cannot
run inside a user transaction AT ALL — SQL Server raises error 574
(SQLSTATE 42000) if you try. A migration file opts out of the transaction
wrapper with a ``-- migrate:no-transaction`` directive: a standalone comment
line matching that exact text ANYWHERE in the file's leading header (the
contiguous run of blank/``--``-comment lines before the first real SQL
statement — not required to be literally line 1, since migrations here open
with long structured rationale comments; see
:func:`_parses_no_transaction_directive`). Such a migration runs every
GO-separated batch, and the ``ops.schema_migrations`` bookkeeping INSERT,
with the connection in AUTOCOMMIT instead: each statement commits itself
immediately. **THERE IS NO ROLLBACK ON THIS PATH** — a failure partway
through leaves every earlier batch permanently applied and the migration
unrecorded (still "pending"). Every no-transaction migration MUST therefore
be written idempotently (an ``IF NOT EXISTS``/``IF EXISTS`` guard on every
statement) so re-running it after a partial failure — or after it already
fully applied — is always safe. This is enforced by author/reviewer
discipline, not by this module. All migrations 001-008 are transactional
(the default) and are unaffected by this option.

Design decisions (documented per the card's "your call, document it"):

  * **ID column width**: every prefixed-ULID id column (``machine_id``,
    ``repository_id``, ``session_id``, ``event_id``, ...) is ``NVARCHAR(64)``
    uniformly, rather than a per-prefix exact width (``mach_`` + 26 = 31,
    ``evt_`` + 26 = 30, ...). A future longer prefix therefore never forces a
    migration; the small extra width is immaterial for an id column.
  * **JSON payloads** (``capabilities``, ``payload``): ``NVARCHAR(MAX)``, not
    SQL Server 2025's native ``json`` type — kept for pyodbc driver simplicity
    this phase (no special bind/fetch handling needed). Revisit once the
    driver's native-json support is verified; flagged again in
    ``database/README.md``.
  * **Runner bookkeeping bootstrap**: schema ``ops`` and table
    ``ops.schema_migrations`` are created by :func:`ensure_bootstrap`
    (idempotent, Python-side), NOT by a numbered migration file — a migration
    can only be *recorded* once that table exists, so it cannot record its own
    creation. This mirrors how Flyway/Alembic self-manage their metadata
    table. Migration 001 still (idempotently) asserts both schemas exist, so
    schema creation remains visible in the versioned migration history.
  * **No foreign keys from ``ops.events`` back to ``registry.*``**: see
    ``database/migrations/003_ops_tables.sql`` for the rationale (an event log
    must not be blocked or cascade-deleted by registry churn).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is a core dep, but stay importable
    pass

if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
    import pyodbc


ENV_VAR = "AGENT_OS_CENTRAL_DB_CONNECTION_STRING"

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Numbered-prefix migration filename: "NNN_description.sql".
_MIGRATION_FILENAME_RE = re.compile(r"^(\d+)_([A-Za-z0-9_]+)\.sql$")

# sqlcmd/SSMS batch separator: a line containing only "GO" (case-insensitive).
# This is NOT real T-SQL - the driver never sees it - so a migration file that
# needs batch isolation (e.g. a future CREATE PROCEDURE, which - like CREATE
# SCHEMA - must be the first statement in its batch) is split on this before
# any batch is executed. Not required by 001-003 (schemas/tables/indexes need
# no batch isolation), but supported now for migrations that will need it.
_GO_SEPARATOR_RE = re.compile(r"^[ \t]*GO[ \t]*$", re.MULTILINE | re.IGNORECASE)

# ODBC SQL type for SQL Server's DATETIMEOFFSET (SQL_SS_TIMESTAMPOFFSET).
SQL_SS_TIMESTAMPOFFSET = -155

# Header directive that opts a migration OUT of the single-transaction
# wrapper (see the module docstring's "Non-transactional migrations"
# section). Matched against a stripped comment line, so leading/trailing
# whitespace around the "--" and the directive text is tolerated.
_NO_TRANSACTION_DIRECTIVE_RE = re.compile(
    r"^--\s*migrate:no-transaction\s*$", re.IGNORECASE
)


def _decode_datetimeoffset(raw: bytes) -> datetime.datetime:
    """pyodbc output converter: SQL_SS_TIMESTAMPOFFSET wire bytes -> aware datetime.

    The wire format is a packed struct: 6 little-endian shorts (year, month,
    day, hour, minute, second), an unsigned int of nanoseconds, then 2 shorts
    for the UTC offset (hours, minutes). This is the standard recipe for this
    well-known pyodbc gap (pyodbc issue #134) and was verified directly against
    this database (``SELECT SYSDATETIMEOFFSET()`` and a round-trip insert/
    select both decode correctly with this converter registered).
    """
    (
        year, month, day, hour, minute, second, nanoseconds,
        offset_hours, offset_minutes,
    ) = struct.unpack("<6hI2h", raw)
    return datetime.datetime(
        year, month, day, hour, minute, second, nanoseconds // 1000,
        datetime.timezone(datetime.timedelta(hours=offset_hours, minutes=offset_minutes)),
    )


class ChecksumMismatchError(RuntimeError):
    """An already-applied migration's on-disk checksum no longer matches the
    checksum recorded when it was applied (drift or tampering)."""


def _require_pyodbc():
    """Import and return the pyodbc module, with a clear error if absent."""
    try:
        import pyodbc
    except ImportError as exc:  # pragma: no cover - exercised when dep missing
        raise SystemExit(
            "Missing dependency: pyodbc. Install with: pip install pyodbc"
        ) from exc
    return pyodbc


def load_connection_string() -> str:
    """Read the central-DB connection string from the environment.

    Checks the process environment (populated by the module-level
    ``load_dotenv()`` above, or by the caller's own shell/CI). Raises
    ``RuntimeError`` naming the env var if unset — never the value.
    """
    value = os.environ.get(ENV_VAR)
    if not value:
        raise RuntimeError(
            f"{ENV_VAR} is not set (checked the process environment and .env). "
            "Central-DB migrations require this connection string."
        )
    return value


def connect(timeout: int = 30) -> "pyodbc.Connection":
    """Open a pyodbc connection to the central DB with autocommit OFF.

    Autocommit is off because migrations are applied transactionally by
    default — one commit-or-rollback per migration file (see
    :func:`apply_migration`). A ``-- migrate:no-transaction`` migration
    (see module docstring) flips ``conn.autocommit`` to ``True`` for the
    duration of that one migration and restores it to ``False`` afterward,
    so this connection-level default holds for every other migration in the
    same run. The DATETIMEOFFSET output converter is always registered (see
    module docstring) so reading any DATETIMEOFFSET column, including the
    runner's own bookkeeping table, never raises.
    """
    pyodbc = _require_pyodbc()
    conn = pyodbc.connect(load_connection_string(), timeout=timeout, autocommit=False)
    conn.add_output_converter(SQL_SS_TIMESTAMPOFFSET, _decode_datetimeoffset)
    return conn


# ---------------------------------------------------------------------------
# Migration discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    """One discovered migration file.

    ``text`` is the file contents read once at discovery (universal-newline
    normalized), so ``checksum`` and :func:`apply_migration` share the single read
    rather than re-reading the file a second time to apply it.
    """

    number: int
    name: str  # filename stem, e.g. "001_create_schemas"
    path: Path
    checksum: str
    text: str
    # True iff the file's header carries "-- migrate:no-transaction" (see
    # module docstring and _parses_no_transaction_directive). Defaults False
    # so every existing migration (001-008), which never sets this, is
    # unaffected.
    no_transaction: bool = False


def compute_checksum(text: str) -> str:
    """sha256 hex digest of migration text.

    ``text`` is expected to come from ``Path.read_text(encoding="utf-8")``,
    which performs universal-newline translation on read (CRLF/CR -> LF) —
    so the checksum is stable across a Windows (CRLF) vs. Linux (LF) checkout
    of the same migration file.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def split_batches(sql_text: str) -> List[str]:
    """Split migration text on standalone ``GO`` lines into executable batches.

    Empty/whitespace-only batches (e.g. leading/trailing separators) are
    dropped. A file with no ``GO`` line yields a single batch containing the
    whole text.
    """
    parts = _GO_SEPARATOR_RE.split(sql_text)
    return [part.strip() for part in parts if part.strip()]


def _parses_no_transaction_directive(text: str) -> bool:
    """Whether ``text``'s header opts out of the single-transaction wrapper.

    The directive is a standalone comment line ``-- migrate:no-transaction``
    ANYWHERE within the migration's leading header — the contiguous run of
    blank and ``--``-comment lines at the top of the file, before the first
    real SQL statement — rather than forced onto literal line 1. This
    matches how migrations in this repo are actually written: every one
    opens with a long structured comment block (rationale, batch layout,
    conventions — see 008's header), and the directive naturally belongs
    alongside that "why". Scanning stops at the first non-blank,
    non-comment line, so an incidental occurrence of this exact text deep
    inside a DDL batch (e.g. a string literal) is never misread as the
    directive.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("--"):
            if _NO_TRANSACTION_DIRECTIVE_RE.match(stripped):
                return True
            continue
        break
    return False


def discover_migrations(migrations_dir: Path) -> List[Migration]:
    """Return every ``NNN_description.sql`` file in ``migrations_dir``, ascending.

    Raises ``ValueError`` on a ``.sql`` file that doesn't match the
    numbered-prefix naming pattern, or on a duplicate migration number, so a
    naming mistake fails loudly instead of silently reordering or shadowing a
    migration.
    """
    migrations: List[Migration] = []
    seen_numbers: Dict[int, str] = {}
    for path in sorted(migrations_dir.glob("*.sql")):
        match = _MIGRATION_FILENAME_RE.match(path.name)
        if not match:
            raise ValueError(
                f"migration file {path.name!r} does not match the required "
                "'NNN_description.sql' naming pattern"
            )
        number = int(match.group(1))
        name = path.stem
        if number in seen_numbers:
            raise ValueError(
                f"duplicate migration number {number}: "
                f"{seen_numbers[number]!r} and {name!r}"
            )
        seen_numbers[number] = name
        text = path.read_text(encoding="utf-8")
        migrations.append(
            Migration(
                number=number, name=name, path=path,
                checksum=compute_checksum(text), text=text,
                no_transaction=_parses_no_transaction_directive(text),
            )
        )
    migrations.sort(key=lambda m: m.number)
    return migrations


# ---------------------------------------------------------------------------
# Bootstrap + bookkeeping (ops.schema_migrations)
# ---------------------------------------------------------------------------

_CHECK_OPS_SCHEMA_SQL = "SELECT 1 FROM sys.schemas WHERE name = 'ops'"
# CREATE SCHEMA must be the first statement in a batch; wrapping it in EXEC()
# of a dynamic string gives it its own implicit batch, so it can sit inside
# this IF guard (a well-known, standard T-SQL idiom).
_CREATE_OPS_SCHEMA_SQL = "EXEC('CREATE SCHEMA ops')"
_CHECK_SCHEMA_MIGRATIONS_TABLE_SQL = (
    "SELECT 1 FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id "
    "WHERE s.name = 'ops' AND t.name = 'schema_migrations'"
)
_CREATE_SCHEMA_MIGRATIONS_TABLE_SQL = """
CREATE TABLE ops.schema_migrations (
    id INT IDENTITY(1,1) NOT NULL,
    name NVARCHAR(255) NOT NULL,
    checksum NVARCHAR(64) NOT NULL,
    applied_at DATETIMEOFFSET NOT NULL
        CONSTRAINT DF_ops_schema_migrations_applied_at DEFAULT (SYSDATETIMEOFFSET()),
    CONSTRAINT PK_ops_schema_migrations PRIMARY KEY (id),
    CONSTRAINT UQ_ops_schema_migrations_name UNIQUE (name)
)
"""
_SELECT_APPLIED_SQL = "SELECT name, checksum, applied_at FROM ops.schema_migrations"
_INSERT_APPLIED_SQL = "INSERT INTO ops.schema_migrations (name, checksum) VALUES (?, ?)"


def ensure_bootstrap(conn: Any) -> None:
    """Idempotently create schema ``ops`` and table ``ops.schema_migrations``.

    This is the runner's OWN bookkeeping object (see module docstring for
    why it is not a numbered migration). Called only from the mutating
    :func:`apply_pending` path — :func:`build_status` and ``--dry-run`` never
    call this, so status/dry-run reporting is always read-only, even against
    a completely virgin database.
    """
    cursor = conn.cursor()
    cursor.execute(_CHECK_OPS_SCHEMA_SQL)
    if cursor.fetchone() is None:
        cursor.execute(_CREATE_OPS_SCHEMA_SQL)
    cursor.execute(_CHECK_SCHEMA_MIGRATIONS_TABLE_SQL)
    if cursor.fetchone() is None:
        cursor.execute(_CREATE_SCHEMA_MIGRATIONS_TABLE_SQL)
    conn.commit()


def _schema_migrations_table_exists(conn: Any) -> bool:
    cursor = conn.cursor()
    cursor.execute(_CHECK_SCHEMA_MIGRATIONS_TABLE_SQL)
    return cursor.fetchone() is not None


def get_applied(conn: Any) -> Dict[str, Dict[str, Any]]:
    """Return ``{migration_name: {"name", "checksum", "applied_at"}}``.

    Read-only and safe to call before :func:`ensure_bootstrap` has ever run:
    if ``ops.schema_migrations`` does not exist yet, returns ``{}`` rather
    than raising, so :func:`build_status` / ``--dry-run`` work against a
    virgin database.
    """
    if not _schema_migrations_table_exists(conn):
        return {}
    cursor = conn.cursor()
    cursor.execute(_SELECT_APPLIED_SQL)
    columns = [d[0] for d in cursor.description]
    return {
        row["name"]: row
        for row in (dict(zip(columns, values)) for values in cursor.fetchall())
    }


# ---------------------------------------------------------------------------
# Status / dry-run (read-only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MigrationStatus:
    """One migration's state relative to what's recorded in the database."""

    number: int
    name: str
    state: str  # "applied" | "pending" | "checksum_mismatch"
    stored_checksum: Optional[str] = None


def build_status(migrations_dir: Path, conn: Any) -> List[MigrationStatus]:
    """Read-only status of every discovered migration against ``conn``.

    Never mutates the database (does not call :func:`ensure_bootstrap`) —
    safe to call at any time, including against a database where schema
    ``ops`` does not exist yet (every migration then reports "pending").
    """
    applied = get_applied(conn)
    statuses: List[MigrationStatus] = []
    for migration in discover_migrations(migrations_dir):
        record = applied.get(migration.name)
        if record is None:
            state, stored = "pending", None
        elif record["checksum"] != migration.checksum:
            state, stored = "checksum_mismatch", record["checksum"]
        else:
            state, stored = "applied", record["checksum"]
        statuses.append(
            MigrationStatus(
                number=migration.number, name=migration.name, state=state, stored_checksum=stored
            )
        )
    return statuses


# ---------------------------------------------------------------------------
# Apply (mutating, transactional)
# ---------------------------------------------------------------------------


def apply_migration(conn: Any, migration: Migration) -> None:
    """Apply one migration file.

    Dispatches on ``migration.no_transaction`` (see module docstring's
    "Non-transactional migrations" section):

    * Transactional (default, every migration 001-008): every GO-separated
      batch is executed, then the bookkeeping row is inserted, all before a
      single commit. Any failure rolls back the whole migration — no
      partial DDL and no bookkeeping row survive a failed migration.
    * Non-transactional (``-- migrate:no-transaction`` header directive):
      every batch and the bookkeeping row are executed with the connection
      in AUTOCOMMIT instead — each one commits itself immediately, and
      THERE IS NO ROLLBACK. Required for SQL Server DDL that cannot run
      inside a user transaction (e.g. CREATE FULLTEXT CATALOG/INDEX).

    Uses the text read once at discovery (``migration.text``).
    """
    if migration.no_transaction:
        _apply_migration_autocommit(conn, migration)
    else:
        _apply_migration_transactional(conn, migration)


def _apply_migration_transactional(conn: Any, migration: Migration) -> None:
    """Default path: one BEGIN-implicit transaction for the whole file."""
    cursor = conn.cursor()
    try:
        for batch in split_batches(migration.text):
            cursor.execute(batch)
        cursor.execute(_INSERT_APPLIED_SQL, migration.name, migration.checksum)
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _apply_migration_autocommit(conn: Any, migration: Migration) -> None:
    """Non-transactional path for a ``-- migrate:no-transaction`` migration.

    Flips ``conn.autocommit`` to ``True`` for the duration of this one
    migration so each batch (and the bookkeeping INSERT) commits itself
    immediately — required for DDL SQL Server refuses to run inside any
    user transaction. Restores ``conn.autocommit`` to ``False`` in a
    ``finally`` (success OR failure) so later migrations applied on this
    same connection in the same run stay transactional by default.

    NO ROLLBACK: on failure partway through, every batch executed before
    the failing one is already permanently committed and the bookkeeping
    row is NOT inserted (the migration stays "pending" on re-run). Safe
    only because the migration file is required to be idempotent (IF NOT
    EXISTS/IF EXISTS guards on every statement) — see module docstring.
    """
    conn.autocommit = True
    try:
        cursor = conn.cursor()
        for batch in split_batches(migration.text):
            cursor.execute(batch)
        cursor.execute(_INSERT_APPLIED_SQL, migration.name, migration.checksum)
    finally:
        conn.autocommit = False


def apply_pending(migrations_dir: Path, conn: Any) -> List[str]:
    """Bootstrap, then apply every not-yet-applied migration in order.

    Raises :class:`ChecksumMismatchError` (before applying anything) if any
    already-applied migration's on-disk checksum no longer matches what was
    recorded — applying further migrations on top of a tampered/edited
    history is refused rather than silently proceeding. Returns the names of
    migrations actually applied by THIS call; an empty list means every
    discovered migration was already applied (idempotent re-run = no-op).
    """
    ensure_bootstrap(conn)
    migrations = discover_migrations(migrations_dir)
    applied = get_applied(conn)

    mismatches = [
        m.name
        for m in migrations
        if m.name in applied and applied[m.name]["checksum"] != m.checksum
    ]
    if mismatches:
        raise ChecksumMismatchError(
            "refusing to apply: on-disk checksum no longer matches the "
            f"recorded checksum for already-applied migration(s): {mismatches}"
        )

    applied_now: List[str] = []
    for migration in migrations:
        if migration.name in applied:
            continue
        apply_migration(conn, migration)
        applied_now.append(migration.name)
    return applied_now


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_status(statuses: List[MigrationStatus]) -> None:
    if not statuses:
        print("(no migrations found)")
        return
    for s in statuses:
        marker = {
            "applied": "applied",
            "pending": "pending",
            "checksum_mismatch": "CHECKSUM MISMATCH",
        }[s.state]
        print(f"{s.name}: {marker}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Apply/inspect Agent OS central-DB migrations "
            "(registry.*/ops.* in agent_os_memory)."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--status", action="store_true",
        help="Report migration status and exit. Read-only; never mutates the database.",
    )
    mode.add_argument(
        "--dry-run", action="store_true",
        help="Show which migrations would be applied. Read-only; never mutates the database.",
    )
    parser.add_argument(
        "--migrations-dir", type=Path, default=DEFAULT_MIGRATIONS_DIR,
        help=f"Override the migrations directory (default: {DEFAULT_MIGRATIONS_DIR}).",
    )
    parser.add_argument("--timeout", type=int, default=30, help="Connection timeout in seconds.")
    args = parser.parse_args(argv)

    conn = connect(timeout=args.timeout)
    try:
        if args.status:
            statuses = build_status(args.migrations_dir, conn)
            _print_status(statuses)
            return 1 if any(s.state == "checksum_mismatch" for s in statuses) else 0

        if args.dry_run:
            statuses = build_status(args.migrations_dir, conn)
            mismatches = [s for s in statuses if s.state == "checksum_mismatch"]
            if mismatches:
                for s in mismatches:
                    print(f"{s.name}: CHECKSUM MISMATCH (would refuse to apply)")
                return 1
            pending = [s for s in statuses if s.state == "pending"]
            if not pending:
                print("Nothing to apply; all migrations already applied.")
            else:
                print("Would apply:")
                for s in pending:
                    print(f"  {s.name}")
            return 0

        # Default action (no flag): apply pending migrations.
        try:
            applied_now = apply_pending(args.migrations_dir, conn)
        except ChecksumMismatchError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        if not applied_now:
            print("Nothing to apply; all migrations already applied.")
        else:
            print(f"Applied {len(applied_now)} migration(s): {', '.join(applied_now)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
