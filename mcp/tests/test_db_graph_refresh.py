"""
Tests for refresh_database_graph(): the TTL / staleness guard (card #1) AND the
in-process pooled-connection build (card #3) + data-only-skip-HTML (card #2).

No live SQL Server or pyodbc is required — the DB layer is fully stubbed.

In-process build seam
---------------------
Since the db_* tool path now builds the graph IN-PROCESS (no subprocess), the
TTL tests mock the in-process build function rather than subprocess.run:

- graph_io._build_db_graph_in_process : the single build seam; one call == one
  rebuild (replaces the previous "two subprocess.run calls per rebuild" shape).
- graph_io.get_repo_root              : returns a controlled temp directory.
- graph_io.find_db_tools_dir          : returns a fake db_tools path (or None).
- graph_io._get_db_graph_ttl          : overrides the TTL without touching env.
- graph_io.log                        : silences log output during tests.

The db_graph.json file is created as a *real file* inside a tempdir so that
Path.exists() / Path.stat() behave naturally.

The build-core / pooled-connection tests drive build_db_graph.build_graph_data
and graph_io's connection pool against a fake pyodbc connection/cursor.
"""
from __future__ import annotations

import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure mcp/ and mcp/db_tools are importable.
_MCP_DIR = Path(__file__).resolve().parent.parent
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))
_DB_TOOLS_DIR = _MCP_DIR / "db_tools"
if str(_DB_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_DB_TOOLS_DIR))

import graph_io  # noqa: E402
import build_db_graph  # noqa: E402

# networkx is an OPTIONAL dependency: the build-core tests that actually
# construct a graph only run when it is installed.  The TTL / wiring tests below
# mock the build seam and never need it, so they always run.
try:
    import networkx as _nx  # noqa: F401
    _HAS_NETWORKX = True
except ImportError:
    _HAS_NETWORKX = False


def _make_graph_json(directory: Path) -> Path:
    """Write a minimal db_graph.json into <directory>/.agent-os/db/ and return its path."""
    db_dir = directory / ".agent-os" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    graph_path = db_dir / "db_graph.json"
    graph_path.write_text('{"nodes": [], "edges": []}')
    return graph_path


def _make_fake_db_tools(tmp: Path) -> Path:
    """Create a fake db_tools dir holding empty build scripts; return its path."""
    fake_db_tools = tmp / "db_tools"
    fake_db_tools.mkdir()
    (fake_db_tools / "build_db_graph.py").write_text("")
    (fake_db_tools / "build_graph_html.py").write_text("")
    return fake_db_tools


# ---------------------------------------------------------------------------
# TTL guard tests (card #1) — retargeted onto the in-process build seam.
# ---------------------------------------------------------------------------


class TestTTLCacheHit(unittest.TestCase):
    """Two refreshes within TTL window: the build seam is invoked zero times."""

    def test_second_call_is_cache_hit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            graph_path = _make_graph_json(tmp)
            graph_path.touch()  # ensure a very recent mtime
            fake_db_tools = _make_fake_db_tools(tmp)

            build_mock = MagicMock()

            with patch.object(graph_io, "_build_db_graph_in_process", build_mock), \
                 patch.object(graph_io, "get_repo_root", return_value=tmp), \
                 patch.object(graph_io, "find_db_tools_dir", return_value=fake_db_tools), \
                 patch.object(graph_io, "_get_db_graph_ttl", return_value=30.0), \
                 patch.object(graph_io, "log"):

                ok1, err1 = graph_io.refresh_database_graph()
                self.assertTrue(ok1)
                self.assertIsNone(err1)
                self.assertEqual(build_mock.call_count, 0,
                                 "First call should be a cache hit; no rebuild expected")

                ok2, err2 = graph_io.refresh_database_graph()
                self.assertTrue(ok2)
                self.assertIsNone(err2)
                self.assertEqual(build_mock.call_count, 0,
                                 "Second call within TTL must not rebuild")


class TestTTLCacheHitAfterRebuild(unittest.TestCase):
    """First call (no cached file) rebuilds once; second call within TTL is a hit."""

    def test_rebuild_then_cache_hit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fake_db_tools = _make_fake_db_tools(tmp)

            db_dir = tmp / ".agent-os" / "db"
            db_dir.mkdir(parents=True)
            graph_path = db_dir / "db_graph.json"

            def build_side_effect(repo_root, include_html=False):
                # Emulate the real build writing the JSON file.
                if not graph_path.exists():
                    graph_path.write_text('{"nodes":[],"edges":[]}')

            build_mock = MagicMock(side_effect=build_side_effect)

            with patch.object(graph_io, "_build_db_graph_in_process", build_mock), \
                 patch.object(graph_io, "get_repo_root", return_value=tmp), \
                 patch.object(graph_io, "find_db_tools_dir", return_value=fake_db_tools), \
                 patch.object(graph_io, "_get_db_graph_ttl", return_value=30.0), \
                 patch.object(graph_io, "log"):

                # First call: no cached file → one in-process build.
                ok1, err1 = graph_io.refresh_database_graph()
                self.assertTrue(ok1)
                self.assertIsNone(err1)
                self.assertEqual(build_mock.call_count, 1,
                                 "Expected exactly one in-process build for a full rebuild")

                # Second call: file now exists with fresh mtime → cache hit.
                ok2, err2 = graph_io.refresh_database_graph()
                self.assertTrue(ok2)
                self.assertIsNone(err2)
                self.assertEqual(build_mock.call_count, 1,
                                 "Second call within TTL must not rebuild again")


class TestTTLExpired(unittest.TestCase):
    """Expired TTL window forces a rebuild."""

    def test_expired_ttl_triggers_rebuild(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            graph_path = _make_graph_json(tmp)

            import os
            old_mtime = time.time() - 60  # back-date 60 s → expired
            os.utime(str(graph_path), (old_mtime, old_mtime))

            fake_db_tools = _make_fake_db_tools(tmp)
            build_mock = MagicMock()

            with patch.object(graph_io, "_build_db_graph_in_process", build_mock), \
                 patch.object(graph_io, "get_repo_root", return_value=tmp), \
                 patch.object(graph_io, "find_db_tools_dir", return_value=fake_db_tools), \
                 patch.object(graph_io, "_get_db_graph_ttl", return_value=30.0), \
                 patch.object(graph_io, "log"):

                ok, err = graph_io.refresh_database_graph()
                self.assertTrue(ok)
                self.assertIsNone(err)
                self.assertEqual(build_mock.call_count, 1,
                                 "Expired TTL must trigger one in-process rebuild")


class TestTTLZeroAlwaysRebuilds(unittest.TestCase):
    """AGENT_OS_DB_GRAPH_TTL=0 disables cache; every call rebuilds."""

    def test_zero_ttl_always_rebuilds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            graph_path = _make_graph_json(tmp)
            graph_path.touch()  # fresh, but TTL=0 must still rebuild
            fake_db_tools = _make_fake_db_tools(tmp)

            build_mock = MagicMock()

            with patch.object(graph_io, "_build_db_graph_in_process", build_mock), \
                 patch.object(graph_io, "get_repo_root", return_value=tmp), \
                 patch.object(graph_io, "find_db_tools_dir", return_value=fake_db_tools), \
                 patch.object(graph_io, "_get_db_graph_ttl", return_value=0.0), \
                 patch.object(graph_io, "log"):

                ok1, _ = graph_io.refresh_database_graph()
                self.assertTrue(ok1)
                self.assertEqual(build_mock.call_count, 1,
                                 "TTL=0 must rebuild on the first call")

                ok2, _ = graph_io.refresh_database_graph()
                self.assertTrue(ok2)
                self.assertEqual(build_mock.call_count, 2,
                                 "TTL=0 must rebuild on every call")


class TestRebuildFailureWithCache(unittest.TestCase):
    """Rebuild failure + cached file exists -> returns (True, warning), no exception."""

    def test_failure_with_cached_file_returns_success_and_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            graph_path = _make_graph_json(tmp)
            import os
            old_mtime = time.time() - 60  # expired → rebuild attempted
            os.utime(str(graph_path), (old_mtime, old_mtime))

            fake_db_tools = _make_fake_db_tools(tmp)

            def build_fail(repo_root, include_html=False):
                raise RuntimeError("db error")

            with patch.object(graph_io, "_build_db_graph_in_process", MagicMock(side_effect=build_fail)), \
                 patch.object(graph_io, "get_repo_root", return_value=tmp), \
                 patch.object(graph_io, "find_db_tools_dir", return_value=fake_db_tools), \
                 patch.object(graph_io, "_get_db_graph_ttl", return_value=30.0), \
                 patch.object(graph_io, "log"):

                ok, msg = graph_io.refresh_database_graph()

            self.assertTrue(ok, "Must return success=True when cached file exists after failure")
            self.assertIsNotNone(msg, "Must return a warning string, not None")
            self.assertIn("cached", msg.lower(),
                          "Warning must mention 'cached' so the caller can surface it")


class TestRebuildFailureWithoutCache(unittest.TestCase):
    """Rebuild failure + no cached file -> returns (False, error_msg)."""

    def test_failure_without_cached_file_returns_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            db_dir = tmp / ".agent-os" / "db"  # no db_graph.json inside
            db_dir.mkdir(parents=True)

            fake_db_tools = _make_fake_db_tools(tmp)

            def build_fail(repo_root, include_html=False):
                raise RuntimeError("fatal error")

            with patch.object(graph_io, "_build_db_graph_in_process", MagicMock(side_effect=build_fail)), \
                 patch.object(graph_io, "get_repo_root", return_value=tmp), \
                 patch.object(graph_io, "find_db_tools_dir", return_value=fake_db_tools), \
                 patch.object(graph_io, "_get_db_graph_ttl", return_value=30.0), \
                 patch.object(graph_io, "log"):

                ok, msg = graph_io.refresh_database_graph()

            self.assertFalse(ok, "Must return success=False when no cached file exists")
            self.assertIsNotNone(msg, "Must return an error string")


class TestToolPathSkipsHtml(unittest.TestCase):
    """Card #2: the in-process tool refresh builds data only — never the HTML."""

    def test_default_refresh_does_not_request_html(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            db_dir = tmp / ".agent-os" / "db"
            db_dir.mkdir(parents=True)
            graph_path = db_dir / "db_graph.json"

            def build_side_effect(repo_root, include_html=False):
                graph_path.write_text('{"nodes":[],"edges":[]}')

            build_mock = MagicMock(side_effect=build_side_effect)
            fake_db_tools = _make_fake_db_tools(tmp)

            with patch.object(graph_io, "_build_db_graph_in_process", build_mock), \
                 patch.object(graph_io, "get_repo_root", return_value=tmp), \
                 patch.object(graph_io, "find_db_tools_dir", return_value=fake_db_tools), \
                 patch.object(graph_io, "_get_db_graph_ttl", return_value=30.0), \
                 patch.object(graph_io, "log"):

                ok, _ = graph_io.refresh_database_graph()  # default tool path
                self.assertTrue(ok)
                build_mock.assert_called_once()
                # The single build call must be data-only (include_html=False).
                _args, kwargs = build_mock.call_args
                self.assertFalse(kwargs.get("include_html", False),
                                 "Tool refresh must not request HTML generation")


# ---------------------------------------------------------------------------
# In-process build core (card #3): build_graph_data over a stubbed pyodbc layer.
# ---------------------------------------------------------------------------


def _canned_schema_rows():
    """A tiny two-table schema with one FK, returned per-query like fetch_rows."""
    tables = [
        {"schema_name": "dbo", "table_name": "Customer", "object_id": 1,
         "create_date": None, "modify_date": None, "is_memory_optimized": 0,
         "temporal_type_desc": "NON_TEMPORAL"},
        {"schema_name": "dbo", "table_name": "Order", "object_id": 2,
         "create_date": None, "modify_date": None, "is_memory_optimized": 0,
         "temporal_type_desc": "NON_TEMPORAL"},
    ]
    columns = [
        {"schema_name": "dbo", "table_name": "Customer", "table_object_id": 1,
         "column_id": 1, "column_name": "Id", "data_type": "int", "max_length": 4,
         "precision": 10, "scale": 0, "is_nullable": 0, "is_identity": 1,
         "is_computed": 0, "default_definition": None, "computed_definition": None,
         "collation_name": None},
        {"schema_name": "dbo", "table_name": "Order", "table_object_id": 2,
         "column_id": 1, "column_name": "Id", "data_type": "int", "max_length": 4,
         "precision": 10, "scale": 0, "is_nullable": 0, "is_identity": 1,
         "is_computed": 0, "default_definition": None, "computed_definition": None,
         "collation_name": None},
        {"schema_name": "dbo", "table_name": "Order", "table_object_id": 2,
         "column_id": 2, "column_name": "CustomerId", "data_type": "int",
         "max_length": 4, "precision": 10, "scale": 0, "is_nullable": 0,
         "is_identity": 0, "is_computed": 0, "default_definition": None,
         "computed_definition": None, "collation_name": None},
    ]
    primary_keys = [
        {"schema_name": "dbo", "table_name": "Customer", "column_name": "Id",
         "constraint_name": "PK_Customer", "key_ordinal": 1},
        {"schema_name": "dbo", "table_name": "Order", "column_name": "Id",
         "constraint_name": "PK_Order", "key_ordinal": 1},
    ]
    foreign_keys = [
        {"foreign_key_name": "FK_Order_Customer", "parent_schema": "dbo",
         "parent_table": "Order", "parent_column": "CustomerId",
         "referenced_schema": "dbo", "referenced_table": "Customer",
         "referenced_column": "Id", "constraint_column_id": 1,
         "delete_referential_action_desc": "NO_ACTION",
         "update_referential_action_desc": "NO_ACTION",
         "is_disabled": 0, "is_not_trusted": 0},
    ]
    routines: list = []
    dependencies: list = []
    return {
        build_db_graph.TABLES_SQL: tables,
        build_db_graph.COLUMNS_SQL: columns,
        build_db_graph.PRIMARY_KEYS_SQL: primary_keys,
        build_db_graph.FOREIGN_KEYS_SQL: foreign_keys,
        build_db_graph.ROUTINES_SQL: routines,
        build_db_graph.DEPENDENCIES_SQL: dependencies,
    }


class _FakeCursor:
    """Mimics enough of pyodbc.Cursor for fetch_rows() over canned rows."""

    def __init__(self, by_sql):
        self._by_sql = by_sql
        self._rows = []
        self.description = []

    def execute(self, sql, *params):
        rows = self._by_sql.get(sql, [])
        self._rows = rows
        # fetch_rows reads column names from cursor.description[*][0].
        if rows:
            cols = list(rows[0].keys())
        else:
            cols = []
        self.description = [(c,) for c in cols]
        return self

    def fetchall(self):
        # fetch_rows zips column names with each row's *values* (ordered as the
        # description columns), so yield value tuples in description order.
        cols = [d[0] for d in self.description]
        return [tuple(row[c] for c in cols) for row in self._rows]

    def close(self):
        pass


class _FakeConnection:
    """Mimics a pyodbc connection; counts cursor() calls."""

    def __init__(self, by_sql):
        self._by_sql = by_sql
        self.cursor_calls = 0
        self.closed = False

    def cursor(self):
        self.cursor_calls += 1
        return _FakeCursor(self._by_sql)

    def close(self):
        self.closed = True


@unittest.skipUnless(_HAS_NETWORKX, "networkx is required to build the graph")
class TestBuildGraphData(unittest.TestCase):
    """build_graph_data() over a stubbed connection yields the expected nodes/edges."""

    def test_nodes_and_edges_from_canned_schema(self):
        conn = _FakeConnection(_canned_schema_rows())
        data = build_db_graph.build_graph_data(conn)

        self.assertTrue(data["directed"])
        self.assertTrue(data["multigraph"])

        node_ids = {n["id"] for n in data["nodes"]}
        self.assertIn("table:dbo.Customer", node_ids)
        self.assertIn("table:dbo.Order", node_ids)
        self.assertIn("column:dbo.Customer.Id", node_ids)
        self.assertIn("column:dbo.Order.CustomerId", node_ids)

        node_types = {n["id"]: n.get("node_type") for n in data["nodes"]}
        self.assertEqual(node_types["table:dbo.Customer"], "Table")
        self.assertEqual(node_types["column:dbo.Order.CustomerId"], "Column")

        rels = {(e["source"], e["target"], e.get("relationship")) for e in data["edges"]}
        # Stores: table -> column
        self.assertIn(("table:dbo.Order", "column:dbo.Order.CustomerId", "Stores"), rels)
        # Links: FK column -> referenced key column
        self.assertIn(
            ("column:dbo.Order.CustomerId", "column:dbo.Customer.Id", "Links"), rels
        )

    def test_matches_build_and_write_json(self):
        """The in-process data dict equals graph_to_json of the same build."""
        conn = _FakeConnection(_canned_schema_rows())
        data = build_db_graph.build_graph_data(conn)

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            conn2 = _FakeConnection(_canned_schema_rows())
            graph = build_db_graph.build_and_write(conn2, out)
            written = build_db_graph.graph_to_json(graph)

        # Ignore the per-run generated_at timestamp in metadata.
        data["metadata"].pop("generated_at", None)
        written["metadata"].pop("generated_at", None)
        self.assertEqual(data, written)


@unittest.skipUnless(_HAS_NETWORKX, "networkx is required to write the graph")
class TestBuildAndWriteOutputs(unittest.TestCase):
    """build_and_write() writes JSON/MD/GraphML and never the HTML."""

    def test_writes_data_files_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "db"
            conn = _FakeConnection(_canned_schema_rows())
            build_db_graph.build_and_write(conn, out)

            self.assertTrue((out / "db_graph.json").exists())
            self.assertTrue((out / "db_graph.md").exists())
            self.assertTrue((out / "db_graph.graphml").exists())
            # The data-only path must never emit the pyvis HTML.
            self.assertFalse((out / "db_graph.html").exists())


# ---------------------------------------------------------------------------
# Pooled connection (card #3): reuse across builds, reconnect on drop.
# ---------------------------------------------------------------------------


class TestPooledConnection(unittest.TestCase):
    """_get_db_connection reuses one handle and reconnects after a drop."""

    def setUp(self):
        graph_io._reset_db_connection()

    def tearDown(self):
        graph_io._reset_db_connection()

    def test_connection_reused_across_two_builds(self):
        connect_calls = {"n": 0}

        def fake_open():
            connect_calls["n"] += 1
            return _FakeConnection(_canned_schema_rows())

        with patch.object(graph_io, "_open_db_connection", side_effect=fake_open), \
             patch.object(graph_io, "_connection_is_alive", return_value=True):
            c1 = graph_io._get_db_connection()
            c2 = graph_io._get_db_connection()

        self.assertIs(c1, c2, "Pooled connection must be reused across calls")
        self.assertEqual(connect_calls["n"], 1, "Two builds must open the connection once")

    def test_dead_connection_triggers_single_reconnect(self):
        connect_calls = {"n": 0}

        def fake_open():
            connect_calls["n"] += 1
            return _FakeConnection(_canned_schema_rows())

        # The first liveness probe (on the cached handle at call 2) reports dead,
        # forcing a single reconnect. Call 1 opens without probing (no handle yet).
        alive_results = iter([False, True])

        def fake_alive(conn):
            try:
                return next(alive_results)
            except StopIteration:
                return True

        with patch.object(graph_io, "_open_db_connection", side_effect=fake_open), \
             patch.object(graph_io, "_connection_is_alive", side_effect=fake_alive):
            first = graph_io._get_db_connection()   # opens (no cached handle yet)
            second = graph_io._get_db_connection()   # cached handle reported dead → reopen

        self.assertEqual(connect_calls["n"], 2,
                         "A dropped connection must reconnect exactly once")
        self.assertIsNot(first, second,
                         "Reconnect must replace the dead handle with a new one")

    def test_in_process_build_resets_pool_on_pyodbc_error(self):
        """A pyodbc error mid-build drops the pooled handle so next call reconnects."""
        # Skip cleanly if pyodbc is not installed (no pyodbc.Error to raise).
        try:
            import pyodbc
        except ImportError:
            self.skipTest("pyodbc not installed")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fake_db_tools = _make_fake_db_tools(tmp)

            bad_conn = _FakeConnection(_canned_schema_rows())

            class _BoomCore:
                @staticmethod
                def build_and_write(conn, output_dir, include_html=False):
                    raise pyodbc.Error("connection reset by peer")

            with patch.object(graph_io, "find_db_tools_dir", return_value=fake_db_tools), \
                 patch.object(graph_io, "_import_build_core", return_value=_BoomCore), \
                 patch.object(graph_io, "_get_db_connection", return_value=bad_conn), \
                 patch.object(graph_io, "_reset_db_connection") as reset_mock:
                with self.assertRaises(pyodbc.Error):
                    graph_io._build_db_graph_in_process(tmp)

            reset_mock.assert_called_once()


# ---------------------------------------------------------------------------
# pyodbc-absent degradation (card #3 criterion 4).
# ---------------------------------------------------------------------------


class TestPyodbcAbsent(unittest.TestCase):
    """With pyodbc unimportable, db refresh fails gracefully; no crash."""

    def test_open_connection_raises_runtimeerror_without_pyodbc(self):
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "pyodbc":
                raise ImportError("No module named 'pyodbc'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaises(RuntimeError) as ctx:
                graph_io._open_db_connection()
        self.assertIn("pyodbc", str(ctx.exception).lower())

    def test_refresh_degrades_gracefully_without_pyodbc(self):
        """refresh_database_graph returns a failure tuple (no exception) sans pyodbc."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            db_dir = tmp / ".agent-os" / "db"  # no cached graph
            db_dir.mkdir(parents=True)
            fake_db_tools = _make_fake_db_tools(tmp)

            def build_raises(repo_root, include_html=False):
                raise RuntimeError(
                    "pyodbc is not installed; the database-graph tools are unavailable"
                )

            with patch.object(graph_io, "_build_db_graph_in_process",
                              MagicMock(side_effect=build_raises)), \
                 patch.object(graph_io, "get_repo_root", return_value=tmp), \
                 patch.object(graph_io, "find_db_tools_dir", return_value=fake_db_tools), \
                 patch.object(graph_io, "_get_db_graph_ttl", return_value=30.0), \
                 patch.object(graph_io, "log"):

                ok, msg = graph_io.refresh_database_graph()

            self.assertFalse(ok, "No pyodbc + no cache must report failure, not crash")
            self.assertIsNotNone(msg)
            self.assertIn("pyodbc", msg.lower())


# ---------------------------------------------------------------------------
# Flask UI refresh path (card #2): the UI still builds the HTML.
# ---------------------------------------------------------------------------


class TestUiRefreshBuildsHtml(unittest.TestCase):
    """app.refresh_db spawns both build_db_graph.py and build_graph_html.py."""

    def test_ui_refresh_invokes_html_builder(self):
        # app.py lives in db_tools; importable via _DB_TOOLS_DIR on sys.path.
        import app as flask_app_module

        _app = flask_app_module.app
        _app.config["TESTING"] = True
        client = _app.test_client()

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)

            run_calls = []

            def fake_run(cmd, *args, **kwargs):
                run_calls.append(cmd)
                result = MagicMock()
                result.stdout = ""
                result.stderr = ""
                result.returncode = 0
                return result

            with patch.object(flask_app_module, "_repo_root_for_slug", return_value=repo_root), \
                 patch.object(flask_app_module, "dotenv_values",
                              return_value={"DB_CONNECTION_STRING": "Driver=fake;"}), \
                 patch.object(flask_app_module.subprocess, "run", side_effect=fake_run):
                resp = client.post(
                    "/myrepo/refresh/db",
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )

        self.assertEqual(resp.status_code, 200)
        joined = " ".join(str(c) for c in run_calls)
        self.assertIn("build_db_graph.py", joined,
                      "UI refresh must build the data graph")
        self.assertIn("build_graph_html.py", joined,
                      "UI refresh must still build the pyvis HTML")


# ---------------------------------------------------------------------------
# TTL parsing (card #1) — unchanged.
# ---------------------------------------------------------------------------


class TestTTLParsing(unittest.TestCase):
    """_get_db_graph_ttl() returns defaults and handles bad env values defensively."""

    def test_default_is_30(self):
        import os
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_OS_DB_GRAPH_TTL", None)
            val = graph_io._get_db_graph_ttl()
        self.assertEqual(val, 30.0)

    def test_valid_value_is_respected(self):
        import os
        with patch.dict(os.environ, {"AGENT_OS_DB_GRAPH_TTL": "60"}):
            val = graph_io._get_db_graph_ttl()
        self.assertEqual(val, 60.0)

    def test_zero_is_respected(self):
        import os
        with patch.dict(os.environ, {"AGENT_OS_DB_GRAPH_TTL": "0"}):
            val = graph_io._get_db_graph_ttl()
        self.assertEqual(val, 0.0)

    def test_bad_value_falls_back_to_default(self):
        import os
        with patch.dict(os.environ, {"AGENT_OS_DB_GRAPH_TTL": "not-a-number"}):
            val = graph_io._get_db_graph_ttl()
        self.assertEqual(val, 30.0)

    def test_empty_string_falls_back_to_default(self):
        import os
        with patch.dict(os.environ, {"AGENT_OS_DB_GRAPH_TTL": ""}):
            val = graph_io._get_db_graph_ttl()
        self.assertEqual(val, 30.0)


if __name__ == "__main__":
    unittest.main()
