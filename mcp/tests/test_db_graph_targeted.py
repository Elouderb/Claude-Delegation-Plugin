"""
Tests for the targeted bounded-neighborhood DB graph build (card #4).

No live SQL Server or pyodbc is required — the DB layer is a fully stubbed,
object_id-keyed fake catalog (_FakeCatalogConnection / _FakeCatalogCursor) that
answers BOTH the entry-resolution queries and the WHERE-filtered ``IN (...)``
per-frontier queries, honoring the bound object_id parameters so that a targeted
build genuinely receives only the in-scope rows.  This is what makes the
"excludes depth-2+ objects" assertions load-bearing: the database (here, the
fake) does the pruning, not a post-filter in Python.

Topology under test (a known FK chain + a routine dependency):

    table A (dbo.A)  --FK-->  table B (dbo.B)  --FK-->  table C (dbo.C)
    procedure R (dbo.R)  --depends on-->  table A

    object_ids: A=10, B=20, C=30, R=40

So from A:
    depth 0 -> {A}
    depth 1 -> {A, B, R}          (A's FK to B; R depends on A)
    depth 2 -> {A, B, R, C}       (B's FK to C)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure mcp/ and mcp/db_tools are importable (mirrors test_db_graph_refresh.py).
_MCP_DIR = Path(__file__).resolve().parent.parent
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))
_DB_TOOLS_DIR = _MCP_DIR / "db_tools"
if str(_DB_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_DB_TOOLS_DIR))

import build_db_graph  # noqa: E402
import db_graph_tools  # noqa: E402
import graph_io  # noqa: E402

try:
    import networkx as _nx  # noqa: F401
    _HAS_NETWORKX = True
except ImportError:
    _HAS_NETWORKX = False


# ---------------------------------------------------------------------------
# Fake catalog: a tiny object_id-keyed schema the filtered queries can walk.
# ---------------------------------------------------------------------------

# object_ids
_A, _B, _C, _R = 10, 20, 30, 40

_TABLES = {
    _A: {"schema_name": "dbo", "table_name": "A"},
    _B: {"schema_name": "dbo", "table_name": "B"},
    _C: {"schema_name": "dbo", "table_name": "C"},
}

_ROUTINES = {
    _R: {"schema_name": "dbo", "object_name": "R", "object_type_code": "P",
         "type_desc": "SQL_STORED_PROCEDURE"},
}

# Columns per table object_id: each table has an Id PK and (for A and B) a FK col.
_COLUMNS = {
    _A: [("Id", 1, True), ("BId", 2, False)],
    _B: [("Id", 1, True), ("CId", 2, False)],
    _C: [("Id", 1, True)],
}

# Foreign keys: (parent_oid, parent_col, ref_oid, ref_col, fk_name)
_FOREIGN_KEYS = [
    (_A, "BId", _B, "Id", "FK_A_B"),
    (_B, "CId", _C, "Id", "FK_B_C"),
]

# Dependencies: (referencing_oid, referenced_table_oid)  R reads table A.
_DEPENDENCIES = [
    (_R, _A),
]


def _table_row(oid: int) -> dict:
    t = _TABLES[oid]
    return {
        "schema_name": t["schema_name"], "table_name": t["table_name"],
        "object_id": oid, "create_date": None, "modify_date": None,
        "is_memory_optimized": 0, "temporal_type_desc": "NON_TEMPORAL",
    }


def _column_rows(oid: int) -> list[dict]:
    # A non-table object_id (e.g. a routine) has no rows in sys.columns — the
    # real WHERE object_id IN (...) returns nothing for it, so do the same.
    t = _TABLES.get(oid)
    if t is None:
        return []
    rows = []
    for name, col_id, _is_pk in _COLUMNS.get(oid, []):
        rows.append({
            "schema_name": t["schema_name"], "table_name": t["table_name"],
            "table_object_id": oid, "column_id": col_id, "column_name": name,
            "data_type": "int", "max_length": 4, "precision": 10, "scale": 0,
            "is_nullable": 0, "is_identity": 1 if _is_pk else 0, "is_computed": 0,
            "default_definition": None, "computed_definition": None,
            "collation_name": None,
        })
    return rows


def _pk_rows(oid: int) -> list[dict]:
    # Non-table object_id → no primary-key rows (mirrors real sys.* behavior).
    t = _TABLES.get(oid)
    if t is None:
        return []
    rows = []
    for name, _col_id, is_pk in _COLUMNS.get(oid, []):
        if is_pk:
            rows.append({
                "schema_name": t["schema_name"], "table_name": t["table_name"],
                "column_name": name, "constraint_name": f"PK_{t['table_name']}",
                "key_ordinal": 1,
            })
    return rows


def _fk_row(parent_oid, parent_col, ref_oid, ref_col, fk_name) -> dict:
    pt, rt = _TABLES[parent_oid], _TABLES[ref_oid]
    return {
        "foreign_key_name": fk_name, "parent_schema": pt["schema_name"],
        "parent_table": pt["table_name"], "parent_column": parent_col,
        "referenced_schema": rt["schema_name"], "referenced_table": rt["table_name"],
        "referenced_column": ref_col, "constraint_column_id": 1,
        "delete_referential_action_desc": "NO_ACTION",
        "update_referential_action_desc": "NO_ACTION",
        "is_disabled": 0, "is_not_trusted": 0,
        "parent_object_id": parent_oid, "referenced_object_id": ref_oid,
    }


def _routine_row(oid: int) -> dict:
    r = _ROUTINES[oid]
    return {
        "schema_name": r["schema_name"], "object_name": r["object_name"],
        "object_id": oid, "object_type_code": r["object_type_code"],
        "type_desc": r["type_desc"], "create_date": None, "modify_date": None,
        "definition": None, "is_schema_bound": 0, "uses_ansi_nulls": 1,
        "uses_quoted_identifier": 1,
    }


def _dep_row(referencing_oid: int, referenced_table_oid: int) -> dict:
    ro = _ROUTINES[referencing_oid]
    tt = _TABLES[referenced_table_oid]
    return {
        "referencing_object_id": referencing_oid,
        "referencing_schema": ro["schema_name"], "referencing_name": ro["object_name"],
        "referencing_type_code": ro["object_type_code"],
        "referenced_id": referenced_table_oid, "referenced_minor_id": 0,
        "referenced_schema": tt["schema_name"], "referenced_table": tt["table_name"],
        "referenced_column": None, "is_schema_bound_reference": 0, "is_ambiguous": 0,
        "referenced_object_id": referenced_table_oid,
    }


class _FakeCatalogCursor:
    """Answers entry-resolution and filtered IN(...) queries over the fake catalog.

    Dispatches on a stable substring of the query template and honors the bound
    object_id parameters, so a frontier query returns only rows for the supplied
    ids — exactly as a real WHERE object_id IN (...) would.
    """

    def __init__(self):
        self._rows: list[dict] = []
        self.description = []
        self.executed: list[tuple[str, tuple]] = []

    # -- helpers --
    def _set_rows(self, rows: list[dict]):
        self._rows = rows
        self.description = [(c,) for c in (rows[0].keys() if rows else [])]

    @staticmethod
    def _ids_from_params(params: tuple) -> list[int]:
        return [p for p in params if isinstance(p, int)]

    def execute(self, sql: str, *params):
        self.executed.append((sql, params))
        ids = set(self._ids_from_params(params))

        # --- entry resolution ---
        if "FROM sys.tables AS t" in sql and "s.name = ?" in sql:
            # RESOLVE_TABLE_OBJECT_ID_SQL: params = (schema, name)
            schema, name = params[0], params[1]
            match = [oid for oid, t in _TABLES.items()
                     if t["schema_name"] == schema and t["table_name"] == name]
            self._set_rows([{"object_id": match[0]}] if match else [])
            return self
        if "FROM sys.objects AS o" in sql and "s.name = ?" in sql:
            # RESOLVE_ROUTINE_OBJECT_ID_SQL: params = (schema, name)
            schema, name = params[0], params[1]
            match = [oid for oid, r in _ROUTINES.items()
                     if r["schema_name"] == schema and r["object_name"] == name]
            self._set_rows([{"object_id": match[0]}] if match else [])
            return self

        # --- filtered per-frontier / final-assembly queries ---
        # Order matters: dispatch on the most specific marker first. The PK query
        # also JOINs sys.columns, so it must be matched (via sys.key_constraints)
        # before the generic columns branch; the columns query is identified by
        # its JOIN sys.types (unique to it).
        if "sys.key_constraints" in sql and "t.object_id IN" in sql:
            rows = []
            for o in sorted(ids):
                rows.extend(_pk_rows(o))
            self._set_rows(rows)
            return self
        if "JOIN sys.types AS ty" in sql and "t.object_id IN" in sql:
            rows = []
            for o in sorted(ids):
                rows.extend(_column_rows(o))
            self._set_rows(rows)
            return self
        if ("FROM sys.tables AS t" in sql and "t.object_id IN" in sql
                and "sys.columns" not in sql):
            self._set_rows([_table_row(o) for o in sorted(ids) if o in _TABLES])
            return self
        if "sys.foreign_keys" in sql and "object_id IN" in sql:
            rows = [_fk_row(*fk) for fk in _FOREIGN_KEYS
                    if fk[0] in ids or fk[2] in ids]
            self._set_rows(rows)
            return self
        if "FROM sys.objects AS o" in sql and "o.object_id IN" in sql:
            self._set_rows([_routine_row(o) for o in sorted(ids) if o in _ROUTINES])
            return self
        if "sys.sql_expression_dependencies" in sql and "object_id IN" in sql:
            rows = [_dep_row(*d) for d in _DEPENDENCIES
                    if d[0] in ids or d[1] in ids]
            self._set_rows(rows)
            return self

        # Unknown query → empty result (keeps the build importable but visible).
        self._set_rows([])
        return self

    def fetchone(self):
        if not self._rows:
            return None
        cols = [d[0] for d in self.description]
        row = self._rows[0]
        return tuple(row[c] for c in cols)

    def fetchall(self):
        cols = [d[0] for d in self.description]
        return [tuple(row[c] for c in cols) for row in self._rows]

    def close(self):
        pass


class _FakeCatalogConnection:
    def __init__(self):
        self.cursors: list[_FakeCatalogCursor] = []

    def cursor(self):
        cur = _FakeCatalogCursor()
        self.cursors.append(cur)
        return cur

    def close(self):
        pass


def _node_ids(data: dict) -> set[str]:
    return {n["id"] for n in data["nodes"]}


# ---------------------------------------------------------------------------
# Build-core scoping tests (card #4 AC 1, 2, 5).
# ---------------------------------------------------------------------------


@unittest.skipUnless(_HAS_NETWORKX, "networkx is required to build the graph")
class TestTargetedScoping(unittest.TestCase):

    def test_depth_one_contains_neighbors_excludes_far(self):
        conn = _FakeCatalogConnection()
        data = build_db_graph.build_targeted_graph_data(
            conn, entry_point="dbo.A", entry_type="table", max_depth=1
        )
        ids = _node_ids(data)

        # Entry A and its depth-1 neighbors B (FK) and R (dependency) present.
        self.assertIn("table:dbo.A", ids)
        self.assertIn("table:dbo.B", ids)
        self.assertIn("procedure:dbo.R", ids)
        # Depth-2 table C must be excluded.
        self.assertNotIn("table:dbo.C", ids)
        # And no column of C leaks in either.
        self.assertFalse(any(i.startswith("column:dbo.C.") for i in ids))

    def test_depth_zero_is_entry_plus_columns_only(self):
        conn = _FakeCatalogConnection()
        data = build_db_graph.build_targeted_graph_data(
            conn, entry_point="dbo.A", entry_type="table", max_depth=0
        )
        ids = _node_ids(data)

        self.assertIn("table:dbo.A", ids)
        self.assertIn("column:dbo.A.Id", ids)
        self.assertIn("column:dbo.A.BId", ids)
        # No neighbors at depth 0.
        self.assertNotIn("table:dbo.B", ids)
        self.assertNotIn("procedure:dbo.R", ids)
        self.assertNotIn("table:dbo.C", ids)
        # The boundary-crossing FK edge (A.BId -> B.Id) is dropped: B absent.
        rels = {(e["source"], e["target"], e.get("relationship")) for e in data["edges"]}
        self.assertNotIn(
            ("column:dbo.A.BId", "column:dbo.B.Id", "Links"), rels
        )

    def test_depth_two_reaches_c(self):
        conn = _FakeCatalogConnection()
        data = build_db_graph.build_targeted_graph_data(
            conn, entry_point="dbo.A", entry_type="table", max_depth=2
        )
        ids = _node_ids(data)
        self.assertIn("table:dbo.C", ids)
        # The A->B Links edge is present (both endpoints in scope).
        rels = {(e["source"], e["target"], e.get("relationship")) for e in data["edges"]}
        self.assertIn(("column:dbo.A.BId", "column:dbo.B.Id", "Links"), rels)

    def test_routine_entry_reaches_referenced_table(self):
        conn = _FakeCatalogConnection()
        data = build_db_graph.build_targeted_graph_data(
            conn, entry_point="dbo.R", entry_type="procedure", max_depth=1
        )
        ids = _node_ids(data)
        self.assertIn("procedure:dbo.R", ids)
        self.assertIn("table:dbo.A", ids)  # R depends on A
        # Creates edge R -> A present.
        rels = {(e["source"], e["target"], e.get("relationship")) for e in data["edges"]}
        self.assertIn(("procedure:dbo.R", "table:dbo.A", "Creates"), rels)

    def test_unknown_entry_returns_empty_graph(self):
        conn = _FakeCatalogConnection()
        data = build_db_graph.build_targeted_graph_data(
            conn, entry_point="dbo.Nope", entry_type="table", max_depth=2
        )
        self.assertEqual(data["nodes"], [])
        self.assertEqual(data["edges"], [])

    def test_bad_entry_type_raises(self):
        conn = _FakeCatalogConnection()
        with self.assertRaises(ValueError):
            build_db_graph.build_targeted_graph_data(
                conn, entry_point="dbo.A", entry_type="widget", max_depth=1
            )

    def test_filtered_query_binds_object_ids(self):
        """The per-frontier queries are parameterized (filtered), not full-pull."""
        conn = _FakeCatalogConnection()
        build_db_graph.build_targeted_graph_data(
            conn, entry_point="dbo.A", entry_type="table", max_depth=1
        )
        # Every filtered catalog query that ran carried bound int object_id params.
        cur = conn.cursors[0]
        filtered = [
            (sql, params) for sql, params in cur.executed
            if "object_id IN" in sql
        ]
        self.assertTrue(filtered, "Expected at least one filtered IN(...) query")
        for _sql, params in filtered:
            self.assertTrue(
                any(isinstance(p, int) for p in params),
                "Filtered query must bind object_id parameters",
            )


# ---------------------------------------------------------------------------
# Back-compat: no entry point => unchanged full build (card #4 AC 4).
# ---------------------------------------------------------------------------


@unittest.skipUnless(_HAS_NETWORKX, "networkx is required to build the graph")
class TestFullBuildUnchanged(unittest.TestCase):

    def test_full_build_includes_every_object(self):
        """build_graph_data (the no-entry-point core) still returns the whole schema."""
        conn = _FakeCatalogConnection()
        # Drive the FULL build via the unfiltered fetch_schema path by stubbing
        # the six full-schema queries to return the entire fake catalog.
        full_rows = {
            build_db_graph.TABLES_SQL: [_table_row(o) for o in _TABLES],
            build_db_graph.COLUMNS_SQL: [r for o in _TABLES for r in _column_rows(o)],
            build_db_graph.PRIMARY_KEYS_SQL: [r for o in _TABLES for r in _pk_rows(o)],
            build_db_graph.FOREIGN_KEYS_SQL: [_fk_row(*fk) for fk in _FOREIGN_KEYS],
            build_db_graph.ROUTINES_SQL: [_routine_row(o) for o in _ROUTINES],
            build_db_graph.DEPENDENCIES_SQL: [_dep_row(*d) for d in _DEPENDENCIES],
        }

        class _FullCursor:
            def __init__(self):
                self._rows = []
                self.description = []

            def execute(self, sql, *params):
                rows = full_rows.get(sql, [])
                self._rows = rows
                self.description = [(c,) for c in (rows[0].keys() if rows else [])]
                return self

            def fetchall(self):
                cols = [d[0] for d in self.description]
                return [tuple(row[c] for c in cols) for row in self._rows]

            def close(self):
                pass

        class _FullConn:
            def cursor(self):
                return _FullCursor()

        data = build_db_graph.build_graph_data(_FullConn())
        ids = _node_ids(data)
        self.assertIn("table:dbo.A", ids)
        self.assertIn("table:dbo.B", ids)
        self.assertIn("table:dbo.C", ids)
        self.assertIn("procedure:dbo.R", ids)


# ---------------------------------------------------------------------------
# Tool routing (card #4 AC 3): each targeted tool passes entry + default depth
# and never writes db_graph.json / uses the TTL cache.
# ---------------------------------------------------------------------------


class TestTargetedToolRouting(unittest.TestCase):
    """The four targeted tools call build_targeted_database_graph correctly and
    never invoke the full refresh()/load() cached flow."""

    def _scoped_dict_for(self, entry_id_predicate):
        # Minimal scoped graph dict using the REAL build id scheme (prefixed),
        # so the tools' strip-prefix matching is genuinely exercised.
        return {
            "nodes": [
                {"id": "table:dbo.A", "type": "Table", "label": "A"},
                {"id": "column:dbo.A.Id", "type": "Column", "label": "Id"},
            ],
            "edges": [
                {"source": "table:dbo.A", "target": "column:dbo.A.Id", "relationship": "Stores"},
            ],
        }

    def test_db_get_table_routes_targeted(self):
        scoped = self._scoped_dict_for(None)
        with patch.object(db_graph_tools, "build_targeted_database_graph",
                          return_value=scoped) as targeted, \
             patch.object(db_graph_tools, "refresh_database_graph") as refresh, \
             patch.object(db_graph_tools, "load_database_graph") as load, \
             patch.object(db_graph_tools, "log"):
            db_graph_tools.db_get_table("dbo.A")

        targeted.assert_called_once()
        args = targeted.call_args.args
        self.assertEqual(args[0], "dbo.A")
        self.assertEqual(args[1], "table")
        self.assertEqual(args[2], db_graph_tools._DEFAULT_TABLE_DEPTH)
        refresh.assert_not_called()
        load.assert_not_called()

    def test_db_get_column_routes_targeted(self):
        scoped = self._scoped_dict_for(None)
        with patch.object(db_graph_tools, "build_targeted_database_graph",
                          return_value=scoped) as targeted, \
             patch.object(db_graph_tools, "refresh_database_graph") as refresh, \
             patch.object(db_graph_tools, "load_database_graph") as load, \
             patch.object(db_graph_tools, "log"):
            db_graph_tools.db_get_column("dbo.A", "Id")

        targeted.assert_called_once()
        args = targeted.call_args.args
        self.assertEqual(args[0], "dbo.A.Id")
        self.assertEqual(args[1], "column")
        self.assertEqual(args[2], db_graph_tools._DEFAULT_COLUMN_DEPTH)
        refresh.assert_not_called()
        load.assert_not_called()

    def test_db_get_table_relationships_routes_targeted(self):
        scoped = self._scoped_dict_for(None)
        with patch.object(db_graph_tools, "build_targeted_database_graph",
                          return_value=scoped) as targeted, \
             patch.object(db_graph_tools, "refresh_database_graph") as refresh, \
             patch.object(db_graph_tools, "load_database_graph") as load, \
             patch.object(db_graph_tools, "log"):
            db_graph_tools.db_get_table_relationships("dbo.A")

        targeted.assert_called_once()
        args = targeted.call_args.args
        self.assertEqual(args[0], "dbo.A")
        self.assertEqual(args[1], "table")
        self.assertEqual(args[2], db_graph_tools._DEFAULT_RELATIONSHIPS_DEPTH)
        refresh.assert_not_called()
        load.assert_not_called()

    def test_db_get_routine_dependencies_routes_targeted(self):
        scoped = {"nodes": [], "edges": []}
        with patch.object(db_graph_tools, "build_targeted_database_graph",
                          return_value=scoped) as targeted, \
             patch.object(db_graph_tools, "refresh_database_graph") as refresh, \
             patch.object(db_graph_tools, "load_database_graph") as load, \
             patch.object(db_graph_tools, "log"):
            db_graph_tools.db_get_routine_dependencies("dbo.R")

        targeted.assert_called_once()
        args = targeted.call_args.args
        self.assertEqual(args[0], "dbo.R")
        self.assertEqual(args[1], "procedure")
        self.assertEqual(args[2], db_graph_tools._DEFAULT_ROUTINE_DEPTH)
        refresh.assert_not_called()
        load.assert_not_called()

    def test_targeted_none_result_is_graph_not_found(self):
        """A None scoped build degrades to the existing 'graph not found' shape."""
        with patch.object(db_graph_tools, "build_targeted_database_graph",
                          return_value=None), \
             patch.object(db_graph_tools, "log"):
            resp = db_graph_tools.db_get_table("dbo.A")
        self.assertIn("Database graph not found", resp["warnings"])


class TestPrefixedIdLookup(unittest.TestCase):
    """Regression for ccf3e8ef: the exact-match tools must find nodes under the
    REAL prefixed id scheme (table:/column:/<routine>:), not just bare names.
    Before the fix these all returned 'not found' in production."""

    _GRAPH = {
        "nodes": [
            {"id": "table:dbo.A", "type": "Table", "label": "A"},
            {"id": "column:dbo.A.Id", "type": "Column", "label": "Id"},
        ],
        "edges": [
            {"source": "table:dbo.A", "target": "column:dbo.A.Id", "relationship": "Stores"},
            {"source": "procedure:dbo.R", "target": "table:dbo.A", "relationship": "Creates"},
            {"source": "procedure:dbo.R", "target": "column:dbo.A.Id", "relationship": "Creates"},
        ],
    }

    def _patch_targeted(self):
        return patch.object(db_graph_tools, "build_targeted_database_graph",
                            return_value=self._GRAPH)

    def test_db_get_table_finds_prefixed_node(self):
        with self._patch_targeted(), patch.object(db_graph_tools, "log"):
            resp = db_graph_tools.db_get_table("dbo.A")
        self.assertFalse(any("not found" in w for w in resp.get("warnings", [])))
        self.assertEqual(resp["results"]["table"]["id"], "table:dbo.A")

    def test_db_get_column_finds_prefixed_node(self):
        with self._patch_targeted(), patch.object(db_graph_tools, "log"):
            resp = db_graph_tools.db_get_column("dbo.A", "Id")
        self.assertFalse(any("not found" in w for w in resp.get("warnings", [])))
        self.assertEqual(resp["results"]["column"]["id"], "column:dbo.A.Id")

    def test_db_get_table_relationships_matches_prefixed_edges(self):
        with self._patch_targeted(), patch.object(db_graph_tools, "log"):
            resp = db_graph_tools.db_get_table_relationships("dbo.A")
        outgoing = resp["results"]["outgoing"]
        self.assertTrue(any(o["target"] == "dbo.A.Id" for o in outgoing))

    def test_db_get_routine_dependencies_classifies_by_prefix(self):
        with self._patch_targeted(), patch.object(db_graph_tools, "log"):
            resp = db_graph_tools.db_get_routine_dependencies("dbo.R")
        deps = resp["results"]["dependencies"]
        self.assertIn("dbo.A", deps["tables"])      # table:dbo.A  -> tables (stripped)
        self.assertIn("dbo.A.Id", deps["columns"])  # column:dbo.A.Id -> columns (stripped)

    def test_table_lookup_does_not_false_match_a_column(self):
        # A column's stripped id ("dbo.A.Id") must not satisfy a table lookup.
        with self._patch_targeted(), patch.object(db_graph_tools, "log"):
            resp = db_graph_tools.db_get_table("dbo.A.Id")
        self.assertTrue(any("not found" in w for w in resp.get("warnings", [])))

    def test_db_find_relationship_path_seeds_prefixed_ids(self):
        # Full-build tool: BFS must seed/goal with table: prefixes (the edge keys
        # are prefixed) and bridge A->B through the FK column Links edge.
        full = {
            "nodes": [],
            "edges": [
                {"source": "table:dbo.A", "target": "column:dbo.A.BId", "relationship": "Stores"},
                {"source": "table:dbo.B", "target": "column:dbo.B.Id", "relationship": "Stores"},
                {"source": "column:dbo.A.BId", "target": "column:dbo.B.Id", "relationship": "Links"},
            ],
        }
        with patch.object(db_graph_tools, "refresh_database_graph", return_value=(True, None)), \
             patch.object(db_graph_tools, "load_database_graph", return_value=full), \
             patch.object(db_graph_tools, "log"):
            resp = db_graph_tools.db_find_relationship_path("dbo.A", "dbo.B")
        paths = resp["results"]["paths"]
        self.assertTrue(paths, "expected a path from dbo.A to dbo.B")
        self.assertEqual(paths[0]["path"][0], "dbo.A")
        self.assertEqual(paths[0]["path"][-1], "dbo.B")


class TestFullToolsStillCached(unittest.TestCase):
    """The two full tools keep the refresh()+load() TTL-cached flow and never
    call the targeted build."""

    def test_db_search_schema_uses_cached_full_flow(self):
        full = {"nodes": [{"id": "table:dbo.A", "type": "Table", "label": "A"}],
                "edges": []}
        with patch.object(db_graph_tools, "refresh_database_graph",
                          return_value=(True, None)) as refresh, \
             patch.object(db_graph_tools, "load_database_graph",
                          return_value=full) as load, \
             patch.object(db_graph_tools, "build_targeted_database_graph") as targeted, \
             patch.object(db_graph_tools, "log"):
            db_graph_tools.db_search_schema("A")

        refresh.assert_called_once()
        load.assert_called_once()
        targeted.assert_not_called()

    def test_db_find_relationship_path_uses_cached_full_flow(self):
        full = {"nodes": [], "edges": []}
        with patch.object(db_graph_tools, "refresh_database_graph",
                          return_value=(True, None)) as refresh, \
             patch.object(db_graph_tools, "load_database_graph",
                          return_value=full) as load, \
             patch.object(db_graph_tools, "build_targeted_database_graph") as targeted, \
             patch.object(db_graph_tools, "log"):
            db_graph_tools.db_find_relationship_path("dbo.A", "dbo.B")

        refresh.assert_called_once()
        load.assert_called_once()
        targeted.assert_not_called()


# ---------------------------------------------------------------------------
# graph_io seam: in-memory only — no file write, no TTL touch (card #4 D).
# ---------------------------------------------------------------------------


class TestGraphIoTargetedSeam(unittest.TestCase):

    def setUp(self):
        graph_io._reset_db_connection()

    def tearDown(self):
        graph_io._reset_db_connection()

    def test_returns_dict_without_writing_or_ttl(self):
        scoped = {"nodes": [{"id": "table:dbo.A"}], "edges": []}

        class _Core:
            @staticmethod
            def build_targeted_graph_data(conn, entry_point, entry_type, max_depth):
                return scoped

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            with patch.object(graph_io, "_import_build_core", return_value=_Core), \
                 patch.object(graph_io, "_get_db_connection", return_value=object()), \
                 patch.object(graph_io, "get_repo_root", return_value=tmp), \
                 patch.object(graph_io, "_get_db_graph_ttl") as ttl_mock, \
                 patch.object(graph_io, "log"):
                result = graph_io.build_targeted_database_graph("dbo.A", "table", 1)

            self.assertEqual(result, scoped)
            # No db_graph.json written by the targeted path.
            self.assertFalse((tmp / ".agent-os" / "db" / "db_graph.json").exists())
            # TTL cache machinery is never consulted for a targeted build.
            ttl_mock.assert_not_called()

    def test_failure_returns_none(self):
        def _boom():
            raise RuntimeError("no build scripts")

        with patch.object(graph_io, "_import_build_core", side_effect=_boom), \
             patch.object(graph_io, "log"):
            result = graph_io.build_targeted_database_graph("dbo.A", "table", 1)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Depth override via AGENT_OS_DB_GRAPH_DEPTH (card #4 E).
# ---------------------------------------------------------------------------


class TestDepthOverride(unittest.TestCase):

    def test_env_overrides_default(self):
        with patch.dict(os.environ, {"AGENT_OS_DB_GRAPH_DEPTH": "3"}):
            self.assertEqual(db_graph_tools._targeted_depth(1), 3)

    def test_blank_falls_back(self):
        with patch.dict(os.environ, {"AGENT_OS_DB_GRAPH_DEPTH": ""}):
            self.assertEqual(db_graph_tools._targeted_depth(2), 2)

    def test_bad_value_falls_back(self):
        with patch.dict(os.environ, {"AGENT_OS_DB_GRAPH_DEPTH": "abc"}):
            self.assertEqual(db_graph_tools._targeted_depth(2), 2)

    def test_negative_falls_back(self):
        with patch.dict(os.environ, {"AGENT_OS_DB_GRAPH_DEPTH": "-1"}):
            self.assertEqual(db_graph_tools._targeted_depth(2), 2)

    def test_zero_is_respected(self):
        with patch.dict(os.environ, {"AGENT_OS_DB_GRAPH_DEPTH": "0"}):
            self.assertEqual(db_graph_tools._targeted_depth(2), 0)


if __name__ == "__main__":
    unittest.main()
