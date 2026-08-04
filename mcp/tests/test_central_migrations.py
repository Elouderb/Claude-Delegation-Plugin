"""Unit tests for database.migrate: the central-DB migration runner.

Card 6f83ab1f. No live SQL Server or pyodbc is required here — every test
below drives the runner against a FAKE pyodbc-shaped connection/cursor (same
style as mcp/tests/test_db_graph_refresh.py's _FakeConnection/_FakeCursor),
keyed off migrate's own SQL constants so the fake never re-derives SQL text
and can't drift from the real queries.

The live round trip (real pyodbc + real agent_os_memory + an
EventEnvelope round trip) is a SEPARATE, env-gated test file:
test_central_migrations_live.py.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from database import migrate  # noqa: E402

REAL_MIGRATIONS_DIR = _REPO_ROOT / "database" / "migrations"


# ---------------------------------------------------------------------------
# Fake pyodbc-shaped connection/cursor
# ---------------------------------------------------------------------------


class _FakeState:
    """Mutable backing store shared by every cursor opened on a connection."""

    def __init__(self) -> None:
        self.ops_schema_exists = False
        self.schema_migrations_exists = False
        self.applied: Dict[str, Dict[str, Any]] = {}
        self.batches_executed: List[str] = []
        self.fail_batches: Set[str] = set()
        self.commits = 0
        self.rollbacks = 0


class _FakeCursor:
    """Mimics enough of pyodbc.Cursor for database.migrate's queries.

    Recognizes migrate's own SQL constants by identity/equality; anything
    else is treated as a migration-file DDL batch (recorded verbatim, or
    raised if pre-marked to fail via state.fail_batches).
    """

    def __init__(self, state: _FakeState) -> None:
        self.state = state
        self._rows: List[tuple] = []
        self.description: List[tuple] = []

    def execute(self, sql: str, *params: Any) -> "_FakeCursor":
        if sql == migrate._CHECK_OPS_SCHEMA_SQL:
            self.description = [("x",)]
            self._rows = [(1,)] if self.state.ops_schema_exists else []
        elif sql == migrate._CREATE_OPS_SCHEMA_SQL:
            self.state.ops_schema_exists = True
            self._rows = []
        elif sql == migrate._CHECK_SCHEMA_MIGRATIONS_TABLE_SQL:
            self.description = [("x",)]
            self._rows = [(1,)] if self.state.schema_migrations_exists else []
        elif sql == migrate._CREATE_SCHEMA_MIGRATIONS_TABLE_SQL:
            self.state.schema_migrations_exists = True
            self._rows = []
        elif sql == migrate._SELECT_APPLIED_SQL:
            self.description = [("name",), ("checksum",), ("applied_at",)]
            self._rows = [
                (r["name"], r["checksum"], r["applied_at"])
                for r in self.state.applied.values()
            ]
        elif sql == migrate._INSERT_APPLIED_SQL:
            name, checksum = params
            self.state.applied[name] = {
                "name": name, "checksum": checksum, "applied_at": "fake-timestamp",
            }
            self._rows = []
        else:
            batch = sql.strip()
            if batch in self.state.fail_batches:
                raise RuntimeError(f"simulated failure executing batch: {batch[:40]!r}")
            self.state.batches_executed.append(batch)
            self._rows = []
        return self

    def fetchone(self) -> Optional[tuple]:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> List[tuple]:
        return self._rows


class _FakeConnection:
    """Mimics a pyodbc connection; tracks commit()/rollback() calls."""

    def __init__(self, state: Optional[_FakeState] = None) -> None:
        self.state = state or _FakeState()

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.state)

    def commit(self) -> None:
        self.state.commits += 1

    def rollback(self) -> None:
        self.state.rollbacks += 1

    def close(self) -> None:
        pass


def _write(tmpdir: Path, filename: str, content: str) -> None:
    (tmpdir / filename).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# split_batches / compute_checksum
# ---------------------------------------------------------------------------


class TestSplitBatches(unittest.TestCase):
    def test_no_go_yields_single_batch(self):
        text = "CREATE TABLE t (id INT);"
        self.assertEqual(migrate.split_batches(text), ["CREATE TABLE t (id INT);"])

    def test_go_separator_splits_case_insensitive(self):
        text = "CREATE TABLE t (id INT);\nGO\nCREATE PROCEDURE p AS SELECT 1;\ngo\nSELECT 2;"
        batches = migrate.split_batches(text)
        self.assertEqual(len(batches), 3)
        self.assertIn("CREATE TABLE t (id INT);", batches)
        self.assertIn("CREATE PROCEDURE p AS SELECT 1;", batches)
        self.assertIn("SELECT 2;", batches)

    def test_empty_batches_dropped(self):
        text = "\nGO\n\nCREATE TABLE t (id INT);\nGO\n\n"
        self.assertEqual(migrate.split_batches(text), ["CREATE TABLE t (id INT);"])

    def test_go_must_be_alone_on_its_line(self):
        # "GOGO" or "GO" as part of a longer token must NOT be treated as a
        # separator.
        text = "SELECT 'GOGONE' AS x;"
        self.assertEqual(migrate.split_batches(text), [text])


class TestChecksum(unittest.TestCase):
    def test_deterministic(self):
        self.assertEqual(migrate.compute_checksum("abc"), migrate.compute_checksum("abc"))

    def test_changes_with_content(self):
        self.assertNotEqual(migrate.compute_checksum("abc"), migrate.compute_checksum("abcd"))

    def test_is_sha256_hex(self):
        digest = migrate.compute_checksum("abc")
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # raises ValueError if not valid hex


# ---------------------------------------------------------------------------
# discover_migrations
# ---------------------------------------------------------------------------


class TestDiscoverMigrations(unittest.TestCase):
    def test_orders_numerically_not_lexicographically(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            # As strings, "10_gamma.sql" sorts before "1_alpha.sql" and
            # "2_beta.sql" (ASCII '0' < '_'); numeric order must still win.
            _write(tmpdir, "10_gamma.sql", "SELECT 3;")
            _write(tmpdir, "1_alpha.sql", "SELECT 1;")
            _write(tmpdir, "2_beta.sql", "SELECT 2;")

            migrations = migrate.discover_migrations(tmpdir)

            self.assertEqual([m.number for m in migrations], [1, 2, 10])
            self.assertEqual(
                [m.name for m in migrations], ["1_alpha", "2_beta", "10_gamma"]
            )

    def test_duplicate_number_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write(tmpdir, "1_alpha.sql", "SELECT 1;")
            _write(tmpdir, "1_alpha_again.sql", "SELECT 1;")

            with self.assertRaises(ValueError):
                migrate.discover_migrations(tmpdir)

    def test_malformed_filename_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write(tmpdir, "not_numbered.sql", "SELECT 1;")

            with self.assertRaises(ValueError):
                migrate.discover_migrations(tmpdir)

    def test_checksum_reflects_file_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write(tmpdir, "1_alpha.sql", "SELECT 1;")
            migrations = migrate.discover_migrations(tmpdir)
            self.assertEqual(migrations[0].checksum, migrate.compute_checksum("SELECT 1;"))

    def test_matches_real_shipped_migrations(self):
        """Sanity check against the actual database/migrations/ this card ships.

        Tolerant, not exact-list, equality (code review, card 93baf05b: the
        previous form hard-coded the full ``[1, 2, 3, 4, 5]`` + name list, so
        every future migration would require editing this test just to keep
        it green). This still catches an accidental deletion, rename, or
        reorder of any migration shipped so far: the known names must still
        be present, as a PREFIX (in their original order) of the full
        contiguously-numbered list — a later migration may only be *appended*
        after them.
        """
        migrations = migrate.discover_migrations(REAL_MIGRATIONS_DIR)
        numbers = [m.number for m in migrations]
        names = [m.name for m in migrations]

        known_names = [
            "001_create_schemas",
            "002_registry_tables",
            "003_ops_tables",
            "004_events_seq_sessions_ended",
            "005_sync_cursors_seq",
        ]
        self.assertGreaterEqual(len(migrations), len(known_names))
        # Contiguous 1..N numbering, no gaps.
        self.assertEqual(numbers, list(range(1, len(migrations) + 1)))
        # Every known migration is still present, in its original order.
        self.assertEqual(names[: len(known_names)], known_names)


# ---------------------------------------------------------------------------
# ensure_bootstrap / get_applied
# ---------------------------------------------------------------------------


class TestEnsureBootstrap(unittest.TestCase):
    def test_creates_schema_and_table_on_virgin_db(self):
        conn = _FakeConnection()
        migrate.ensure_bootstrap(conn)
        self.assertTrue(conn.state.ops_schema_exists)
        self.assertTrue(conn.state.schema_migrations_exists)
        self.assertEqual(conn.state.commits, 1)

    def test_idempotent_second_call_creates_nothing_new(self):
        conn = _FakeConnection()
        migrate.ensure_bootstrap(conn)
        migrate.ensure_bootstrap(conn)
        # Both flags stay True; no error, no duplicate-create attempted (the
        # fake would not detect a duplicate CREATE itself, but the guarded
        # IF NOT EXISTS check means execute() is only asked to run the
        # CREATE_* statements when the corresponding flag is False).
        self.assertTrue(conn.state.ops_schema_exists)
        self.assertTrue(conn.state.schema_migrations_exists)
        self.assertEqual(conn.state.commits, 2)


class TestGetApplied(unittest.TestCase):
    def test_empty_when_table_missing_no_mutation(self):
        conn = _FakeConnection()
        applied = migrate.get_applied(conn)
        self.assertEqual(applied, {})
        # Read-only: must not have created anything.
        self.assertFalse(conn.state.ops_schema_exists)
        self.assertFalse(conn.state.schema_migrations_exists)

    def test_returns_recorded_migrations(self):
        conn = _FakeConnection()
        conn.state.schema_migrations_exists = True
        conn.state.applied["001_create_schemas"] = {
            "name": "001_create_schemas", "checksum": "abc123", "applied_at": "t0",
        }
        applied = migrate.get_applied(conn)
        self.assertEqual(set(applied), {"001_create_schemas"})
        self.assertEqual(applied["001_create_schemas"]["checksum"], "abc123")


# ---------------------------------------------------------------------------
# build_status (read-only)
# ---------------------------------------------------------------------------


class TestBuildStatus(unittest.TestCase):
    def test_all_pending_on_virgin_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write(tmpdir, "1_alpha.sql", "SELECT 1;")
            _write(tmpdir, "2_beta.sql", "SELECT 2;")
            conn = _FakeConnection()

            statuses = migrate.build_status(tmpdir, conn)

            self.assertEqual([s.state for s in statuses], ["pending", "pending"])
            # Read-only: status must never bootstrap the database.
            self.assertFalse(conn.state.ops_schema_exists)

    def test_mixed_applied_and_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write(tmpdir, "1_alpha.sql", "SELECT 1;")
            _write(tmpdir, "2_beta.sql", "SELECT 2;")
            checksum = migrate.compute_checksum("SELECT 1;")

            conn = _FakeConnection()
            conn.state.schema_migrations_exists = True
            conn.state.applied["1_alpha"] = {
                "name": "1_alpha", "checksum": checksum, "applied_at": "t0",
            }

            statuses = migrate.build_status(tmpdir, conn)
            by_name = {s.name: s.state for s in statuses}
            self.assertEqual(by_name["1_alpha"], "applied")
            self.assertEqual(by_name["2_beta"], "pending")

    def test_checksum_mismatch_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write(tmpdir, "1_alpha.sql", "SELECT 1;")

            conn = _FakeConnection()
            conn.state.schema_migrations_exists = True
            conn.state.applied["1_alpha"] = {
                "name": "1_alpha", "checksum": "stale-checksum-deadbeef", "applied_at": "t0",
            }

            statuses = migrate.build_status(tmpdir, conn)
            self.assertEqual(statuses[0].state, "checksum_mismatch")
            self.assertEqual(statuses[0].stored_checksum, "stale-checksum-deadbeef")


# ---------------------------------------------------------------------------
# apply_pending / apply_migration (mutating)
# ---------------------------------------------------------------------------


class TestApplyPending(unittest.TestCase):
    def test_applies_in_numeric_order_and_records_each(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write(tmpdir, "1_alpha.sql", "CREATE TABLE alpha (id INT);")
            _write(tmpdir, "2_beta.sql", "CREATE TABLE beta (id INT);")
            conn = _FakeConnection()

            applied_now = migrate.apply_pending(tmpdir, conn)

            self.assertEqual(applied_now, ["1_alpha", "2_beta"])
            self.assertEqual(
                conn.state.batches_executed,
                ["CREATE TABLE alpha (id INT);", "CREATE TABLE beta (id INT);"],
            )
            self.assertEqual(set(conn.state.applied), {"1_alpha", "2_beta"})
            # One commit for bootstrap + one per migration.
            self.assertEqual(conn.state.commits, 3)
            self.assertEqual(conn.state.rollbacks, 0)

    def test_idempotent_rerun_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write(tmpdir, "1_alpha.sql", "CREATE TABLE alpha (id INT);")
            conn = _FakeConnection()

            first = migrate.apply_pending(tmpdir, conn)
            batches_after_first = list(conn.state.batches_executed)
            second = migrate.apply_pending(tmpdir, conn)

            self.assertEqual(first, ["1_alpha"])
            self.assertEqual(second, [], "re-run must apply nothing new")
            self.assertEqual(
                conn.state.batches_executed, batches_after_first,
                "re-run must not execute any migration batch again",
            )

    def test_checksum_mismatch_blocks_apply_before_any_batch_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write(tmpdir, "1_alpha.sql", "CREATE TABLE alpha (id INT);")
            _write(tmpdir, "2_beta.sql", "CREATE TABLE beta (id INT);")

            conn = _FakeConnection()
            conn.state.schema_migrations_exists = True
            conn.state.ops_schema_exists = True
            conn.state.applied["1_alpha"] = {
                "name": "1_alpha", "checksum": "tampered-checksum", "applied_at": "t0",
            }

            with self.assertRaises(migrate.ChecksumMismatchError):
                migrate.apply_pending(tmpdir, conn)

            self.assertEqual(
                conn.state.batches_executed, [],
                "no migration batch may run once a checksum mismatch is detected",
            )

    def test_failed_batch_rolls_back_and_does_not_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write(tmpdir, "1_alpha.sql", "CREATE TABLE alpha (id INT);")
            conn = _FakeConnection()
            conn.state.fail_batches.add("CREATE TABLE alpha (id INT);")

            with self.assertRaises(RuntimeError):
                migrate.apply_pending(tmpdir, conn)

            self.assertNotIn("1_alpha", conn.state.applied)
            self.assertEqual(conn.state.rollbacks, 1)

    def test_second_migration_failure_does_not_undo_the_first(self):
        """Transactional PER MIGRATION, not per run: migration 1's commit
        stands even if migration 2 subsequently fails."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            _write(tmpdir, "1_alpha.sql", "CREATE TABLE alpha (id INT);")
            _write(tmpdir, "2_beta.sql", "CREATE TABLE beta (id INT);")
            conn = _FakeConnection()
            conn.state.fail_batches.add("CREATE TABLE beta (id INT);")

            with self.assertRaises(RuntimeError):
                migrate.apply_pending(tmpdir, conn)

            self.assertIn("1_alpha", conn.state.applied)
            self.assertNotIn("2_beta", conn.state.applied)
            self.assertEqual(conn.state.rollbacks, 1)


if __name__ == "__main__":
    unittest.main()
