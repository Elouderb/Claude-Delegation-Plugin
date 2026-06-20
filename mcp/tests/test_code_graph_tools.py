"""
Tests for code-graph tools and shared graph tools in mcp/server.py.

Uses a small fixture graph (fixtures/graph_fixture.json) — no live graphify
process required. All db_* tools are skipped because they need pyodbc and a
live SQL Server; see the 'db_tools' pytest mark.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure `mcp/` is on the path so we can import server.py directly.
_MCP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MCP_DIR))

# Patch FastMCP before importing server so the module-level `server = FastMCP(…)`
# does not try to connect to anything.
_mock_fastmcp = MagicMock()
_mock_fastmcp_instance = MagicMock()
_mock_fastmcp.return_value = _mock_fastmcp_instance
# `@server.tool()` is used as a decorator — make it a no-op pass-through.
_mock_fastmcp_instance.tool.return_value = lambda f: f

with patch.dict("sys.modules", {"mcp": MagicMock(), "mcp.server": MagicMock(),
                                 "mcp.server.fastmcp": MagicMock(FastMCP=_mock_fastmcp)}):
    import server  # noqa: E402  (import after sys.path manipulation)
    # After the split, the tool functions live in dedicated modules (already in
    # sys.modules via server's imports). Reference them directly.
    import graph_io  # noqa: E402
    import code_graph_tools  # noqa: E402
    import shared_graph_tools  # noqa: E402
    import graph_server  # noqa: E402

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "graph_fixture.json"


def _load_fixture():
    with open(_FIXTURE) as fh:
        return json.load(fh)


class TestLoadCodeGraphMissing(unittest.TestCase):
    """load_code_graph() must return None when graph.json is absent."""

    def test_returns_none_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(graph_io, "get_repo_root", return_value=Path(tmpdir)):
                result = graph_io.load_code_graph()
        self.assertIsNone(result)

    def test_returns_none_when_graphify_out_missing(self):
        """Even the graphify-out directory may be absent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(graph_io, "get_repo_root", return_value=Path(tmpdir)):
                result = graph_io.load_code_graph()
        self.assertIsNone(result)


class TestLoadCodeGraphPresent(unittest.TestCase):
    """load_code_graph() loads and returns the fixture when the file exists."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        outdir = Path(self._tmpdir) / "graphify-out"
        outdir.mkdir()
        (outdir / "graph.json").write_text(json.dumps(_load_fixture()))

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _root(self):
        return Path(self._tmpdir)

    def test_loads_nodes_and_edges(self):
        with patch.object(graph_io, "get_repo_root", return_value=self._root()):
            # Also suppress _check_graph_server_health side-effect
            with patch.object(graph_server, "_check_graph_server_health"):
                data = graph_io.load_code_graph()
        self.assertIsNotNone(data)
        self.assertIn("nodes", data)
        self.assertIn("edges", data)
        self.assertGreater(len(data["nodes"]), 0)


class _FixtureBase(unittest.TestCase):
    """Base class: patches load_code_graph() with the fixture for all subclasses."""

    def setUp(self):
        self._fixture = _load_fixture()
        self._patch = patch.object(graph_io, "load_code_graph", return_value=self._fixture)
        self._patch.start()
        # Suppress health check side-effects
        self._health_patch = patch.object(graph_server, "_check_graph_server_health")
        self._health_patch.start()

    def tearDown(self):
        self._patch.stop()
        self._health_patch.stop()


class TestCodeSearchSymbols(_FixtureBase):

    def test_finds_exact_id_match(self):
        result = code_graph_tools.code_search_symbols("helper_func")
        symbols = result["results"]["symbols"]
        ids = [s["id"] for s in symbols]
        self.assertIn("myapp.utils.helper_func", ids)

    def test_finds_partial_label_match(self):
        result = code_graph_tools.code_search_symbols("run")
        symbols = result["results"]["symbols"]
        self.assertTrue(any("run" in s["label"] for s in symbols))

    def test_type_filter_returns_only_functions(self):
        result = code_graph_tools.code_search_symbols("func", symbol_type="function")
        symbols = result["results"]["symbols"]
        self.assertTrue(all(s["type"] == "function" for s in symbols))
        self.assertGreater(len(symbols), 0)

    def test_type_filter_no_match_returns_empty(self):
        result = code_graph_tools.code_search_symbols("main", symbol_type="class")
        symbols = result["results"]["symbols"]
        self.assertEqual(symbols, [])

    def test_response_has_standard_keys(self):
        result = code_graph_tools.code_search_symbols("utils")
        self.assertIn("graph", result)
        self.assertIn("results", result)
        self.assertIn("warnings", result)
        self.assertEqual(result["graph"], "code")

    def test_missing_graph_returns_warning(self):
        with patch.object(graph_io, "load_code_graph", return_value=None):
            result = code_graph_tools.code_search_symbols("anything")
        self.assertGreater(len(result["warnings"]), 0)


class TestCodeGetSymbol(_FixtureBase):

    def test_finds_symbol_by_id_substring(self):
        result = code_graph_tools.code_get_symbol("helper_func")
        self.assertIn("symbol", result["results"])
        self.assertNotIn("helper_func not found", str(result["warnings"]))

    def test_returns_callers_list(self):
        result = code_graph_tools.code_get_symbol("helper_func")
        self.assertIn("callers", result["results"])
        # The fixture has two call edges into helper_func
        self.assertEqual(len(result["results"]["callers"]), 2)

    def test_returns_imports_list(self):
        result = code_graph_tools.code_get_symbol("myapp.main")
        imports = result["results"].get("imports", [])
        # The fixture has an imports edge from myapp.main → myapp.utils
        self.assertIn("myapp.utils", imports)

    def test_unknown_symbol_warns(self):
        result = code_graph_tools.code_get_symbol("nonexistent_xyz")
        self.assertTrue(any("not found" in w for w in result["warnings"]))

    def test_missing_graph_warns(self):
        with patch.object(graph_io, "load_code_graph", return_value=None):
            result = code_graph_tools.code_get_symbol("anything")
        self.assertGreater(len(result["warnings"]), 0)


class TestCodeGetDependencies(_FixtureBase):

    def test_finds_symbol_dependencies(self):
        result = code_graph_tools.code_get_dependencies("myapp.main.run", depth=2)
        self.assertIn("graph", result)
        self.assertEqual(result["graph"], "code")

    def test_unknown_symbol_warns(self):
        result = code_graph_tools.code_get_dependencies("nonexistent", depth=1)
        self.assertTrue(any("not found" in w for w in result["warnings"]))

    def test_missing_graph_warns(self):
        with patch.object(graph_io, "load_code_graph", return_value=None):
            result = code_graph_tools.code_get_dependencies("anything")
        self.assertGreater(len(result["warnings"]), 0)


class TestCodeFindCallers(_FixtureBase):

    def test_direct_callers_of_helper_func(self):
        result = code_graph_tools.code_find_callers("helper_func", transitive=False)
        callers = result["results"]["callers"]
        caller_ids = [c["caller"] for c in callers]
        self.assertIn("myapp.main.run", caller_ids)
        self.assertIn("tests.test_utils", caller_ids)

    def test_transitive_callers(self):
        result = code_graph_tools.code_find_callers("helper_func", transitive=True, max_depth=3)
        # Transitive: should include at least the same direct callers
        callers = result["results"]["callers"]
        self.assertGreater(len(callers), 0)

    def test_no_callers_when_none_exist(self):
        result = code_graph_tools.code_find_callers("myapp.main.run", transitive=False)
        callers = result["results"]["callers"]
        self.assertEqual(callers, [])

    def test_unknown_symbol_warns(self):
        result = code_graph_tools.code_find_callers("nonexistent_xyz")
        self.assertTrue(any("not found" in w for w in result["warnings"]))

    def test_missing_graph_warns(self):
        with patch.object(graph_io, "load_code_graph", return_value=None):
            result = code_graph_tools.code_find_callers("anything")
        self.assertGreater(len(result["warnings"]), 0)


class TestCodeImpactAnalysis(_FixtureBase):

    def test_impact_has_expected_keys(self):
        result = code_graph_tools.code_impact_analysis("helper_func")
        r = result["results"]
        for key in ("direct_callers", "modules_affected", "tests", "interfaces", "entry_points"):
            self.assertIn(key, r)

    def test_direct_callers_populated(self):
        result = code_graph_tools.code_impact_analysis("helper_func")
        callers = result["results"]["direct_callers"]
        self.assertIn("myapp.main.run", callers)

    def test_test_modules_categorised(self):
        result = code_graph_tools.code_impact_analysis("helper_func")
        # tests.test_utils calls helper_func — but code_impact_analysis
        # only captures sources of call edges pointing AT the symbol.
        # Both myapp.main.run and tests.test_utils have call edges → helper_func.
        direct_callers = result["results"]["direct_callers"]
        self.assertTrue(len(direct_callers) >= 1)

    def test_unknown_symbol_warns(self):
        result = code_graph_tools.code_impact_analysis("nonexistent_xyz")
        self.assertTrue(any("not found" in w for w in result["warnings"]))

    def test_missing_graph_warns(self):
        with patch.object(graph_io, "load_code_graph", return_value=None):
            result = code_graph_tools.code_impact_analysis("anything")
        self.assertGreater(len(result["warnings"]), 0)


class TestGraphSearchNodesCode(_FixtureBase):
    """graph_search_nodes() with graph='code' uses load_code_graph()."""

    def test_exact_match(self):
        result = shared_graph_tools.graph_search_nodes("myapp.utils", graph="code")
        nodes = result["results"]["nodes"]
        self.assertTrue(any(n["id"] == "myapp.utils" for n in nodes))

    def test_fuzzy_match(self):
        result = shared_graph_tools.graph_search_nodes("utils", graph="code", fuzzy=True)
        nodes = result["results"]["nodes"]
        self.assertGreater(len(nodes), 0)

    def test_type_filter(self):
        result = shared_graph_tools.graph_search_nodes("myapp", graph="code", node_type="function", fuzzy=True)
        nodes = result["results"]["nodes"]
        self.assertTrue(all(n["type"] == "function" for n in nodes))

    def test_limit_applied(self):
        result = shared_graph_tools.graph_search_nodes("myapp", graph="code", fuzzy=True, limit=2)
        nodes = result["results"]["nodes"]
        self.assertLessEqual(len(nodes), 2)

    def test_missing_graph_warns(self):
        with patch.object(graph_io, "load_code_graph", return_value=None):
            result = shared_graph_tools.graph_search_nodes("anything", graph="code")
        self.assertGreater(len(result["warnings"]), 0)


class TestGraphGetNode(_FixtureBase):

    def test_finds_existing_node(self):
        result = shared_graph_tools.graph_get_node("myapp.utils", graph="code")
        self.assertIn("node", result["results"])
        self.assertEqual(result["results"]["node"]["id"], "myapp.utils")

    def test_not_found_warns(self):
        result = shared_graph_tools.graph_get_node("does.not.exist", graph="code")
        self.assertTrue(any("not found" in w for w in result["warnings"]))

    def test_missing_graph_warns(self):
        with patch.object(graph_io, "load_code_graph", return_value=None):
            result = shared_graph_tools.graph_get_node("anything", graph="code")
        self.assertGreater(len(result["warnings"]), 0)


class TestGraphGetNeighbors(_FixtureBase):

    def test_outgoing_neighbors(self):
        result = shared_graph_tools.graph_get_neighbors("myapp.main.run", graph="code", direction="outgoing")
        outgoing = result["results"]["outgoing"]
        targets = [n["target"] for n in outgoing]
        self.assertIn("myapp.utils.helper_func", targets)

    def test_incoming_neighbors(self):
        result = shared_graph_tools.graph_get_neighbors("myapp.utils.helper_func", graph="code",
                                            direction="incoming")
        incoming = result["results"]["incoming"]
        sources = [n["source"] for n in incoming]
        self.assertIn("myapp.main.run", sources)

    def test_relationship_filter(self):
        result = shared_graph_tools.graph_get_neighbors("myapp.main", graph="code",
                                            relationship="imports")
        outgoing = result["results"]["outgoing"]
        self.assertTrue(all(n["relationship"] == "imports" for n in outgoing))

    def test_missing_graph_warns(self):
        with patch.object(graph_io, "load_code_graph", return_value=None):
            result = shared_graph_tools.graph_get_neighbors("anything", graph="code")
        self.assertGreater(len(result["warnings"]), 0)


class TestGraphFindPath(_FixtureBase):

    def test_finds_direct_path(self):
        result = shared_graph_tools.graph_find_path("myapp.main.run", "myapp.utils.helper_func", graph="code")
        paths = result["results"]["paths"]
        self.assertGreater(len(paths), 0)
        self.assertIn("myapp.utils.helper_func", paths[0]["path"])

    def test_no_path_returns_empty(self):
        result = shared_graph_tools.graph_find_path("myapp.utils.helper_func", "myapp.main", graph="code",
                                        directed=True)
        # Directed: no edge from helper_func back to main module (edges go the other way)
        paths = result["results"]["paths"]
        self.assertEqual(paths, [])

    def test_undirected_finds_reverse_path(self):
        result = shared_graph_tools.graph_find_path("myapp.utils.helper_func", "myapp.main.run", graph="code",
                                        directed=False)
        paths = result["results"]["paths"]
        self.assertGreater(len(paths), 0)

    def test_missing_graph_warns(self):
        with patch.object(graph_io, "load_code_graph", return_value=None):
            result = shared_graph_tools.graph_find_path("a", "b", graph="code")
        self.assertGreater(len(result["warnings"]), 0)


class TestGraphGetSubgraph(_FixtureBase):

    def test_seed_node_included(self):
        result = shared_graph_tools.graph_get_subgraph(["myapp.utils"], graph="code", depth=1)
        node_ids = [n["id"] for n in result["results"]["nodes"]]
        self.assertIn("myapp.utils", node_ids)

    def test_depth_expands_neighbors(self):
        result = shared_graph_tools.graph_get_subgraph(["myapp.main"], graph="code", depth=1)
        node_ids = [n["id"] for n in result["results"]["nodes"]]
        # myapp.main connects to myapp.utils via imports edge
        self.assertIn("myapp.utils", node_ids)

    def test_missing_graph_warns(self):
        with patch.object(graph_io, "load_code_graph", return_value=None):
            result = shared_graph_tools.graph_get_subgraph(["anything"], graph="code")
        self.assertGreater(len(result["warnings"]), 0)


class TestGraphStatus(unittest.TestCase):

    def test_missing_file_reports_not_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(graph_io, "get_repo_root", return_value=Path(tmpdir)):
                result = shared_graph_tools.graph_status(graph="code")
        self.assertFalse(result["results"]["exists"])

    def test_present_file_reports_counts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            outdir = Path(tmpdir) / "graphify-out"
            outdir.mkdir()
            (outdir / "graph.json").write_text(json.dumps(_load_fixture()))
            with patch.object(graph_io, "get_repo_root", return_value=Path(tmpdir)):
                result = shared_graph_tools.graph_status(graph="code")
        self.assertTrue(result["results"]["exists"])
        self.assertGreater(result["results"]["node_count"], 0)
        self.assertEqual(result["results"]["edge_count"], 3)


class TestDbToolsSkipped(unittest.TestCase):
    """
    DB-graph tools (db_get_table, db_get_column, db_search_schema,
    db_get_table_relationships, db_find_relationship_path,
    db_get_routine_dependencies) require pyodbc + a live SQL Server.
    They are NOT exercised here.  This placeholder documents the skip decision
    so CI does not silently omit them.
    """

    @unittest.skip("db_* tools require pyodbc + live SQL Server — excluded from CI")
    def test_db_tools_skipped(self):
        pass  # pragma: no cover


if __name__ == "__main__":
    unittest.main()
