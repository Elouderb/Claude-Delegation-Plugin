"""LIVE integration test: applies database/migrations/* to the real central DB
(agent_os_memory) and round-trips a contracts.events.EventEnvelope example
through ops.events.

Card 6f83ab1f, test requirement: "one integration test gated on
AGENT_OS_CENTRAL_DB_CONNECTION_STRING presence (skips cleanly when absent —
db_* suite precedent — so CI stays green)". No SQL Server is available in CI,
so this test SKIPS there; it is exercised for real only where the env var (and
a reachable SQL Server) are present.

Never prints, logs, or asserts on the connection string itself — only on
schema/object names, counts, and the (non-secret) EventEnvelope payload.

Cleanup: the EventEnvelope round-trip inserts exactly one row (the fixed
example id from contracts.examples) into ops.events and DELETEs it again
before the test ends (success or failure), leaving agent_os_memory with only
the migrated schema/objects — no residual test data.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from database import migrate  # noqa: E402

# DOUBLE-gated: a connection string AND an explicit opt-in are BOTH required.
# These tests call migrate.apply_pending against the REAL DB, so presence of a
# connection string alone is unsafe (a plain full-suite run would silently migrate
# production). Opt in deliberately with AGENT_OS_RUN_LIVE_DB_TESTS=1.
_HAS_CENTRAL_DB = bool(os.environ.get(migrate.ENV_VAR)) and (
    os.environ.get("AGENT_OS_RUN_LIVE_DB_TESTS") == "1"
)

EXPECTED_TABLES = {
    ("registry", "machines"),
    ("registry", "repositories"),
    ("registry", "agent_sessions"),
    ("registry", "tasks"),  # migration 006 (Phase 2a task down-projection)
    ("ops", "events"),
    ("ops", "sync_cursors"),
    ("ops", "ingest_errors"),
    ("ops", "schema_migrations"),
}

EXPECTED_EVENTS_INDEXES = {
    "IX_ops_events_type_occurred": ["event_type", "occurred_at"],
    "IX_ops_events_machine_occurred": ["machine_id", "occurred_at"],
    "IX_ops_events_repository_occurred": ["repository_id", "occurred_at"],
}

_LIST_TABLES_SQL = """
SELECT s.name AS schema_name, t.name AS table_name
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE s.name IN ('registry', 'ops')
ORDER BY s.name, t.name
"""

_LIST_EVENTS_INDEX_COLUMNS_SQL = """
SELECT i.name AS index_name, c.name AS column_name, ic.key_ordinal
FROM sys.indexes i
JOIN sys.tables t ON t.object_id = i.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE s.name = 'ops' AND t.name = 'events' AND i.is_primary_key = 0
ORDER BY i.name, ic.key_ordinal
"""


@unittest.skipUnless(
    _HAS_CENTRAL_DB,
    f"live central-DB migration test requires {migrate.ENV_VAR} + AGENT_OS_RUN_LIVE_DB_TESTS=1",
)
class TestLiveCentralMigrations(unittest.TestCase):
    """Applies the real migrations to the real agent_os_memory and verifies them."""

    @classmethod
    def setUpClass(cls):
        cls.conn = migrate.connect(timeout=30)
        # Apply (idempotent: a no-op if a prior run already applied everything).
        cls.applied_this_run = migrate.apply_pending(migrate.DEFAULT_MIGRATIONS_DIR, cls.conn)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_status_reports_no_pending_and_no_mismatches_after_apply(self):
        statuses = migrate.build_status(migrate.DEFAULT_MIGRATIONS_DIR, self.conn)
        self.assertTrue(statuses, "expected at least one discovered migration")
        states = {s.name: s.state for s in statuses}
        self.assertTrue(
            all(state == "applied" for state in states.values()),
            f"expected every migration applied, got: {states}",
        )

    def test_expected_tables_exist(self):
        cursor = self.conn.cursor()
        cursor.execute(_LIST_TABLES_SQL)
        found = {(row.schema_name, row.table_name) for row in cursor.fetchall()}
        self.assertEqual(
            found, EXPECTED_TABLES,
            f"registry.*/ops.* tables mismatch. found={sorted(found)}",
        )

    def test_expected_events_indexes_exist_with_correct_columns(self):
        cursor = self.conn.cursor()
        cursor.execute(_LIST_EVENTS_INDEX_COLUMNS_SQL)
        by_index: dict[str, list[str]] = {}
        for row in cursor.fetchall():
            by_index.setdefault(row.index_name, []).append(row.column_name)
        for name, expected_columns in EXPECTED_EVENTS_INDEXES.items():
            self.assertIn(name, by_index, f"missing index {name} on ops.events")
            self.assertEqual(
                by_index[name], expected_columns,
                f"index {name} has unexpected column order/composition",
            )

    def test_apply_pending_again_is_a_noop(self):
        """Idempotence, proven live: re-running apply_pending applies nothing."""
        applied_again = migrate.apply_pending(migrate.DEFAULT_MIGRATIONS_DIR, self.conn)
        self.assertEqual(applied_again, [])

    def test_event_envelope_round_trips_into_ops_events(self):
        """Insert the contracts/examples EventEnvelope, read it back, parse it,
        then clean up the row so no test data is left behind."""
        from contracts.examples import EXAMPLES
        from contracts.events import EventEnvelope
        from database import events as db_events

        example = EXAMPLES["EventEnvelope"]
        self.assertIsInstance(example, EventEnvelope)
        # Plain `assert isinstance` (not self.assertIsInstance) so static type
        # checkers narrow `example` from AgentOSModel to EventEnvelope below —
        # EXAMPLES is typed Dict[str, AgentOSModel], so without this narrowing
        # every .event_id/.event_type/... access below is a type error.
        assert isinstance(example, EventEnvelope)

        # Clean up any leftover row from a prior interrupted run of this same
        # test before inserting, so a re-run is never blocked by its own PK.
        db_events.delete_event(self.conn, example.event_id)
        self.conn.commit()

        try:
            db_events.insert_event_envelope(self.conn, example)
            self.conn.commit()

            round_tripped = db_events.fetch_event_envelope(self.conn, example.event_id)
            self.assertIsNotNone(round_tripped, "inserted event was not found on read-back")
            # Plain `assert` (not self.assertIsNotNone) so static type checkers
            # narrow round_tripped from Optional[EventEnvelope] to EventEnvelope.
            assert round_tripped is not None
            self.assertEqual(round_tripped.event_id, example.event_id)
            self.assertEqual(round_tripped.event_type, example.event_type)
            self.assertEqual(round_tripped.occurred_at, example.occurred_at)
            self.assertEqual(round_tripped.machine_id, example.machine_id)
            self.assertEqual(round_tripped.agent_id, example.agent_id)
            self.assertEqual(round_tripped.session_id, example.session_id)
            self.assertEqual(round_tripped.project_id, example.project_id)
            self.assertEqual(round_tripped.repository_id, example.repository_id)
            self.assertEqual(round_tripped.task_id, example.task_id)
            self.assertEqual(round_tripped.payload, example.payload)
            self.assertEqual(round_tripped.schema_version, example.schema_version)
        finally:
            # Always clean up the round-trip row, pass or fail.
            db_events.delete_event(self.conn, example.event_id)
            self.conn.commit()

        # Verify cleanup actually took effect.
        gone = db_events.fetch_event_envelope(self.conn, example.event_id)
        self.assertIsNone(gone, "round-trip test row was not cleaned up")

    def test_non_utc_occurred_at_stores_the_correct_instant(self):
        """Regression for the DATETIMEOFFSET offset-drop bug (finding 3).

        pyodbc binds an aware datetime to DATETIMEOFFSET by sending its
        wall-clock digits with a +00:00 offset, dropping the datetime's own
        offset. database.events normalizes occurred_at to UTC before binding, so
        a non-UTC occurred_at must round-trip to the SAME INSTANT (not the same
        wall-clock digits mislabeled +00:00)."""
        from datetime import datetime, timedelta, timezone

        from contracts import EVENT, MACHINE, new_id
        from contracts.events import EventEnvelope
        from database import events as db_events

        # 12:00 at +05:30 == 06:30:00Z. The buggy path would store 12:00Z.
        occurred = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        envelope = EventEnvelope(
            event_id=new_id(EVENT),
            event_type="task.created",
            occurred_at=occurred,
            machine_id=new_id(MACHINE),
            payload={"tz": "+05:30"},
        )
        db_events.delete_event(self.conn, envelope.event_id)
        self.conn.commit()
        try:
            db_events.insert_event_envelope(self.conn, envelope)
            self.conn.commit()
            got = db_events.fetch_event_envelope(self.conn, envelope.event_id)
            assert got is not None
            # Aware-datetime equality compares the INSTANT, so this passes only if
            # the stored moment is 06:30Z, not 12:00Z.
            self.assertEqual(got.occurred_at, occurred)
            self.assertEqual(
                got.occurred_at.astimezone(timezone.utc),
                datetime(2026, 8, 3, 6, 30, 0, tzinfo=timezone.utc),
            )
        finally:
            db_events.delete_event(self.conn, envelope.event_id)
            self.conn.commit()

    def test_insert_event_if_absent_is_idempotent(self):
        """Regression for finding 5: a redelivered event (same event_id) must be
        a no-op, not a PK-violation batch poison."""
        from contracts import EVENT, MACHINE, new_id, utc_now
        from contracts.events import EventEnvelope
        from database import events as db_events

        envelope = EventEnvelope(
            event_id=new_id(EVENT),
            event_type="task.updated",
            occurred_at=utc_now(),
            machine_id=new_id(MACHINE),
            payload={"dup": True},
        )
        db_events.delete_event(self.conn, envelope.event_id)
        self.conn.commit()
        try:
            first = db_events.insert_event_if_absent(self.conn, envelope)
            self.conn.commit()
            second = db_events.insert_event_if_absent(self.conn, envelope)
            self.conn.commit()
            self.assertTrue(first, "first insert should report inserted=True")
            self.assertFalse(second, "duplicate insert should report inserted=False")
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM ops.events WHERE event_id = ?", envelope.event_id
            )
            count_row = cursor.fetchone()
            assert count_row is not None
            self.assertEqual(count_row[0], 1, "exactly one row must exist")
        finally:
            db_events.delete_event(self.conn, envelope.event_id)
            self.conn.commit()


if __name__ == "__main__":
    unittest.main()
