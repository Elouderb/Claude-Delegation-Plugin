#!/usr/bin/env python3
"""
Build a live, local directed graph of a Microsoft SQL Server database schema.

Each run connects to SQL Server, reads the current catalog metadata, rebuilds the
entire graph from scratch, and overwrites the output files.

Node types:
    - Table
    - Column
    - Function
    - Procedure

Relationship types:
    - Stores:  Table -> Column
    - Links:   Foreign-key Column -> Referenced key Column
    - Creates: Procedure/Function -> referenced Table or Column

Important:
    SQL Server's dependency catalog reports objects referenced by procedures and
    functions. This script labels those dependency edges "Creates" to match the
    requested graph vocabulary, but the edge means "the routine has a recorded
    SQL dependency on this table/column"; it does not prove that the routine
    executes DDL that physically creates that object.

Outputs:
    - db_graph.graphml  (portable graph file)
    - db_graph.json     (easy for agents and applications to consume)
    - db_graph.md       (human/Claude-readable summary)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is a core dep, but stay importable
    pass

# networkx and pyodbc are OPTIONAL dependencies of the database-graph subsystem.
# They are imported lazily inside the functions that actually need them so that
# this module stays importable (e.g. by graph_io's in-process build path, or by
# the test suite) even when neither package is installed.  When a build is
# actually attempted without them, a clear error is raised at that point.
if TYPE_CHECKING:  # pragma: no cover - import for type checkers only
    import networkx as nx
    import pyodbc


def _require_networkx():
    """Import and return the networkx module, with a clear error if absent."""
    try:
        import networkx as nx
    except ImportError as exc:  # pragma: no cover - exercised when dep missing
        raise SystemExit(
            "Missing dependency: networkx. Install with: pip install networkx"
        ) from exc
    return nx


def _require_pyodbc():
    """Import and return the pyodbc module, with a clear error if absent."""
    try:
        import pyodbc
    except ImportError as exc:  # pragma: no cover - exercised when dep missing
        raise SystemExit(
            "Missing dependency: pyodbc. Install with: pip install pyodbc"
        ) from exc
    return pyodbc


TABLES_SQL = """
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    t.object_id,
    t.create_date,
    t.modify_date,
    t.is_memory_optimized,
    t.temporal_type_desc
FROM sys.tables AS t
JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
WHERE t.is_ms_shipped = 0
ORDER BY s.name, t.name;
"""

COLUMNS_SQL = """
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    t.object_id AS table_object_id,
    c.column_id,
    c.name AS column_name,
    ty.name AS data_type,
    CASE
        WHEN ty.name IN ('nchar', 'nvarchar') AND c.max_length > 0
            THEN c.max_length / 2
        ELSE c.max_length
    END AS max_length,
    c.precision,
    c.scale,
    c.is_nullable,
    c.is_identity,
    c.is_computed,
    dc.definition AS default_definition,
    cc.definition AS computed_definition,
    c.collation_name
FROM sys.tables AS t
JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
JOIN sys.columns AS c
    ON c.object_id = t.object_id
JOIN sys.types AS ty
    ON ty.user_type_id = c.user_type_id
LEFT JOIN sys.default_constraints AS dc
    ON dc.parent_object_id = c.object_id
   AND dc.parent_column_id = c.column_id
LEFT JOIN sys.computed_columns AS cc
    ON cc.object_id = c.object_id
   AND cc.column_id = c.column_id
WHERE t.is_ms_shipped = 0
ORDER BY s.name, t.name, c.column_id;
"""

PRIMARY_KEYS_SQL = """
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    c.name AS column_name,
    kc.name AS constraint_name,
    ic.key_ordinal
FROM sys.key_constraints AS kc
JOIN sys.tables AS t
    ON t.object_id = kc.parent_object_id
JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
JOIN sys.index_columns AS ic
    ON ic.object_id = t.object_id
   AND ic.index_id = kc.unique_index_id
JOIN sys.columns AS c
    ON c.object_id = ic.object_id
   AND c.column_id = ic.column_id
WHERE kc.type = 'PK'
  AND t.is_ms_shipped = 0
ORDER BY s.name, t.name, ic.key_ordinal;
"""

FOREIGN_KEYS_SQL = """
SELECT
    fk.name AS foreign_key_name,
    ps.name AS parent_schema,
    pt.name AS parent_table,
    pc.name AS parent_column,
    rs.name AS referenced_schema,
    rt.name AS referenced_table,
    rc.name AS referenced_column,
    fkc.constraint_column_id,
    fk.delete_referential_action_desc,
    fk.update_referential_action_desc,
    fk.is_disabled,
    fk.is_not_trusted
FROM sys.foreign_keys AS fk
JOIN sys.foreign_key_columns AS fkc
    ON fkc.constraint_object_id = fk.object_id
JOIN sys.tables AS pt
    ON pt.object_id = fkc.parent_object_id
JOIN sys.schemas AS ps
    ON ps.schema_id = pt.schema_id
JOIN sys.columns AS pc
    ON pc.object_id = fkc.parent_object_id
   AND pc.column_id = fkc.parent_column_id
JOIN sys.tables AS rt
    ON rt.object_id = fkc.referenced_object_id
JOIN sys.schemas AS rs
    ON rs.schema_id = rt.schema_id
JOIN sys.columns AS rc
    ON rc.object_id = fkc.referenced_object_id
   AND rc.column_id = fkc.referenced_column_id
WHERE pt.is_ms_shipped = 0
  AND rt.is_ms_shipped = 0
ORDER BY ps.name, pt.name, fk.name, fkc.constraint_column_id;
"""

ROUTINES_SQL = """
SELECT
    s.name AS schema_name,
    o.name AS object_name,
    o.object_id,
    o.type AS object_type_code,
    o.type_desc,
    o.create_date,
    o.modify_date,
    sm.definition,
    sm.is_schema_bound,
    sm.uses_ansi_nulls,
    sm.uses_quoted_identifier
FROM sys.objects AS o
JOIN sys.schemas AS s
    ON s.schema_id = o.schema_id
LEFT JOIN sys.sql_modules AS sm
    ON sm.object_id = o.object_id
WHERE o.is_ms_shipped = 0
  AND o.type IN (
      'P', 'PC',
      'FN', 'IF', 'TF', 'FS', 'FT'
  )
ORDER BY s.name, o.name;
"""

DEPENDENCIES_SQL = """
SELECT DISTINCT
    ro.object_id AS referencing_object_id,
    rs.name AS referencing_schema,
    ro.name AS referencing_name,
    ro.type AS referencing_type_code,
    sed.referenced_id,
    sed.referenced_minor_id,
    COALESCE(sed.referenced_schema_name, ts.name) AS referenced_schema,
    COALESCE(sed.referenced_entity_name, tt.name) AS referenced_table,
    tc.name AS referenced_column,
    sed.is_schema_bound_reference,
    sed.is_ambiguous
FROM sys.sql_expression_dependencies AS sed
JOIN sys.objects AS ro
    ON ro.object_id = sed.referencing_id
JOIN sys.schemas AS rs
    ON rs.schema_id = ro.schema_id
LEFT JOIN sys.tables AS tt
    ON tt.object_id = sed.referenced_id
LEFT JOIN sys.schemas AS ts
    ON ts.schema_id = tt.schema_id
LEFT JOIN sys.columns AS tc
    ON tc.object_id = sed.referenced_id
   AND tc.column_id = sed.referenced_minor_id
WHERE ro.is_ms_shipped = 0
  AND ro.type IN (
      'P', 'PC',
      'FN', 'IF', 'TF', 'FS', 'FT'
  )
  AND tt.object_id IS NOT NULL
ORDER BY
    rs.name,
    ro.name,
    referenced_schema,
    referenced_table,
    referenced_column;
"""

PROCEDURE_TYPES = {"P", "PC"}
FUNCTION_TYPES = {"FN", "IF", "TF", "FS", "FT"}

# ---------------------------------------------------------------------------
# Targeted (bounded-neighborhood) build — entry resolution + filtered queries
# ---------------------------------------------------------------------------
#
# The full-schema queries above pull the ENTIRE catalog.  For a targeted build
# we instead walk outward from a single entry object to a bounded depth, issuing
# WHERE-filtered variants of the same catalog queries per BFS frontier so the
# database does the pruning (filtered query, not full-pull-then-filter).  Every
# catalog object is keyed by its integer ``object_id``; the BFS therefore tracks
# a frontier of object_ids and expands it over FK edges (sys.foreign_keys) and
# routine/object dependency edges (sys.sql_expression_dependencies).
#
# The rows produced by these filtered queries have the SAME column shape as the
# full-build rows, so the assembled row lists feed the UNCHANGED build_graph() +
# graph_to_json() and yield a strict subset of the full graph with an identical
# node/edge schema.
#
# Valid entry_type values for build_targeted_graph_data().
TARGETED_ENTRY_TYPES = ("table", "column", "function", "procedure")

# --- Entry-point resolution -------------------------------------------------
# Resolve a schema-qualified table / routine name to its object_id.  Used for
# entry_type in {table, function, procedure} (a column entry resolves its parent
# table's object_id and remembers the column for depth-0 scoping).

RESOLVE_TABLE_OBJECT_ID_SQL = """
SELECT t.object_id
FROM sys.tables AS t
JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
WHERE t.is_ms_shipped = 0
  AND s.name = ?
  AND t.name = ?;
"""

RESOLVE_ROUTINE_OBJECT_ID_SQL = """
SELECT o.object_id
FROM sys.objects AS o
JOIN sys.schemas AS s
    ON s.schema_id = o.schema_id
WHERE o.is_ms_shipped = 0
  AND s.name = ?
  AND o.name = ?
  AND o.type IN ('P', 'PC', 'FN', 'IF', 'TF', 'FS', 'FT');
"""

# --- Per-frontier filtered catalog queries ---------------------------------
# Each appends an ``IN (...)`` clause sized to the current frontier (see
# _in_clause / _fetch_rows_in).  They are otherwise identical in column shape to
# the corresponding full-schema query so build_graph() consumes them unchanged.

TABLES_FILTERED_SQL = """
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    t.object_id,
    t.create_date,
    t.modify_date,
    t.is_memory_optimized,
    t.temporal_type_desc
FROM sys.tables AS t
JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
WHERE t.is_ms_shipped = 0
  AND t.object_id IN ({placeholders})
ORDER BY s.name, t.name;
"""

COLUMNS_FILTERED_SQL = """
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    t.object_id AS table_object_id,
    c.column_id,
    c.name AS column_name,
    ty.name AS data_type,
    CASE
        WHEN ty.name IN ('nchar', 'nvarchar') AND c.max_length > 0
            THEN c.max_length / 2
        ELSE c.max_length
    END AS max_length,
    c.precision,
    c.scale,
    c.is_nullable,
    c.is_identity,
    c.is_computed,
    dc.definition AS default_definition,
    cc.definition AS computed_definition,
    c.collation_name
FROM sys.tables AS t
JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
JOIN sys.columns AS c
    ON c.object_id = t.object_id
JOIN sys.types AS ty
    ON ty.user_type_id = c.user_type_id
LEFT JOIN sys.default_constraints AS dc
    ON dc.parent_object_id = c.object_id
   AND dc.parent_column_id = c.column_id
LEFT JOIN sys.computed_columns AS cc
    ON cc.object_id = c.object_id
   AND cc.column_id = c.column_id
WHERE t.is_ms_shipped = 0
  AND t.object_id IN ({placeholders})
ORDER BY s.name, t.name, c.column_id;
"""

PRIMARY_KEYS_FILTERED_SQL = """
SELECT
    s.name AS schema_name,
    t.name AS table_name,
    c.name AS column_name,
    kc.name AS constraint_name,
    ic.key_ordinal
FROM sys.key_constraints AS kc
JOIN sys.tables AS t
    ON t.object_id = kc.parent_object_id
JOIN sys.schemas AS s
    ON s.schema_id = t.schema_id
JOIN sys.index_columns AS ic
    ON ic.object_id = t.object_id
   AND ic.index_id = kc.unique_index_id
JOIN sys.columns AS c
    ON c.object_id = ic.object_id
   AND c.column_id = ic.column_id
WHERE kc.type = 'PK'
  AND t.is_ms_shipped = 0
  AND t.object_id IN ({placeholders})
ORDER BY s.name, t.name, ic.key_ordinal;
"""

# Foreign keys touching the frontier in EITHER direction (parent table in the
# frontier OR referenced table in the frontier).  Returning both lets the BFS
# discover neighbours upstream and downstream of the frontier tables.
FOREIGN_KEYS_FILTERED_SQL = """
SELECT
    fk.name AS foreign_key_name,
    ps.name AS parent_schema,
    pt.name AS parent_table,
    pc.name AS parent_column,
    rs.name AS referenced_schema,
    rt.name AS referenced_table,
    rc.name AS referenced_column,
    fkc.constraint_column_id,
    fk.delete_referential_action_desc,
    fk.update_referential_action_desc,
    fk.is_disabled,
    fk.is_not_trusted,
    pt.object_id AS parent_object_id,
    rt.object_id AS referenced_object_id
FROM sys.foreign_keys AS fk
JOIN sys.foreign_key_columns AS fkc
    ON fkc.constraint_object_id = fk.object_id
JOIN sys.tables AS pt
    ON pt.object_id = fkc.parent_object_id
JOIN sys.schemas AS ps
    ON ps.schema_id = pt.schema_id
JOIN sys.columns AS pc
    ON pc.object_id = fkc.parent_object_id
   AND pc.column_id = fkc.parent_column_id
JOIN sys.tables AS rt
    ON rt.object_id = fkc.referenced_object_id
JOIN sys.schemas AS rs
    ON rs.schema_id = rt.schema_id
JOIN sys.columns AS rc
    ON rc.object_id = fkc.referenced_object_id
   AND rc.column_id = fkc.referenced_column_id
WHERE pt.is_ms_shipped = 0
  AND rt.is_ms_shipped = 0
  AND (pt.object_id IN ({placeholders}) OR rt.object_id IN ({placeholders}))
ORDER BY ps.name, pt.name, fk.name, fkc.constraint_column_id;
"""

ROUTINES_FILTERED_SQL = """
SELECT
    s.name AS schema_name,
    o.name AS object_name,
    o.object_id,
    o.type AS object_type_code,
    o.type_desc,
    o.create_date,
    o.modify_date,
    sm.definition,
    sm.is_schema_bound,
    sm.uses_ansi_nulls,
    sm.uses_quoted_identifier
FROM sys.objects AS o
JOIN sys.schemas AS s
    ON s.schema_id = o.schema_id
LEFT JOIN sys.sql_modules AS sm
    ON sm.object_id = o.object_id
WHERE o.is_ms_shipped = 0
  AND o.type IN ('P', 'PC', 'FN', 'IF', 'TF', 'FS', 'FT')
  AND o.object_id IN ({placeholders})
ORDER BY s.name, o.name;
"""

# Dependency edges touching the frontier in EITHER direction (the referencing
# routine in the frontier OR the referenced table in the frontier).  This lets
# the BFS reach a routine from a table it reads, and a table from a routine that
# reads it.
DEPENDENCIES_FILTERED_SQL = """
SELECT DISTINCT
    ro.object_id AS referencing_object_id,
    rs.name AS referencing_schema,
    ro.name AS referencing_name,
    ro.type AS referencing_type_code,
    sed.referenced_id,
    sed.referenced_minor_id,
    COALESCE(sed.referenced_schema_name, ts.name) AS referenced_schema,
    COALESCE(sed.referenced_entity_name, tt.name) AS referenced_table,
    tc.name AS referenced_column,
    sed.is_schema_bound_reference,
    sed.is_ambiguous,
    tt.object_id AS referenced_object_id
FROM sys.sql_expression_dependencies AS sed
JOIN sys.objects AS ro
    ON ro.object_id = sed.referencing_id
JOIN sys.schemas AS rs
    ON rs.schema_id = ro.schema_id
LEFT JOIN sys.tables AS tt
    ON tt.object_id = sed.referenced_id
LEFT JOIN sys.schemas AS ts
    ON ts.schema_id = tt.schema_id
LEFT JOIN sys.columns AS tc
    ON tc.object_id = sed.referenced_id
   AND tc.column_id = sed.referenced_minor_id
WHERE ro.is_ms_shipped = 0
  AND ro.type IN ('P', 'PC', 'FN', 'IF', 'TF', 'FS', 'FT')
  AND tt.object_id IS NOT NULL
  AND (ro.object_id IN ({placeholders}) OR tt.object_id IN ({placeholders}))
ORDER BY
    rs.name,
    ro.name,
    referenced_schema,
    referenced_table,
    referenced_column;
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a fresh SQL Server schema graph using pyodbc and NetworkX."
    )
    parser.add_argument(
        "--connection-string",
        default=os.getenv("DB_CONNECTION_STRING"),
        help=(
            "pyodbc SQL Server connection string. "
            "Defaults to the DB_CONNECTION_STRING environment variable."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=".agent-os/db",
        help="Directory for generated graph files. Default: .agent-os/db",
    )
    parser.add_argument(
        "--include-definitions",
        action="store_true",
        help="Include full procedure/function SQL definitions in node attributes.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Connection timeout in seconds. Default: 30",
    )
    parser.add_argument(
        "--entry-point",
        default=None,
        help=(
            "Build only the bounded neighborhood around this object instead of "
            "the whole schema. Identifier form: 'schema.object' for a table, "
            "function, or procedure; 'schema.table.column' for a column. "
            "With no --entry-point, the full schema is built (default)."
        ),
    )
    parser.add_argument(
        "--entry-type",
        choices=list(TARGETED_ENTRY_TYPES),
        default=None,
        help=(
            "Kind of object --entry-point names: table, column, function, or "
            "procedure. Required when --entry-point is given."
        ),
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=1,
        help=(
            "For a targeted (--entry-point) build, the number of relationship "
            "hops to expand outward over FK and routine-dependency edges. "
            "0 = the entry object only (plus its own columns for a table). "
            "Default: 1. Ignored for a full build."
        ),
    )
    return parser.parse_args()


def fetch_rows(cursor: pyodbc.Cursor, sql: str) -> list[dict[str, Any]]:
    cursor.execute(sql)
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def graphml_safe(value: Any) -> str | int | float | bool:
    if value is None:
        return ""
    safe = json_safe(value)
    if isinstance(safe, (str, int, float, bool)):
        return safe
    return json.dumps(safe, sort_keys=True)


def compact_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {key: json_safe(value) for key, value in data.items() if value is not None}


def table_id(schema: str, table: str) -> str:
    return f"table:{schema}.{table}"


def column_id(schema: str, table: str, column: str) -> str:
    return f"column:{schema}.{table}.{column}"


def routine_id(node_type: str, schema: str, name: str) -> str:
    return f"{node_type.lower()}:{schema}.{name}"


def sql_type_display(column: dict[str, Any]) -> str:
    data_type = str(column["data_type"])
    max_length = column.get("max_length")
    precision = column.get("precision")
    scale = column.get("scale")

    if data_type in {"varchar", "nvarchar", "char", "nchar", "binary", "varbinary"}:
        if max_length == -1:
            return f"{data_type}(max)"
        if max_length is not None:
            return f"{data_type}({max_length})"

    if data_type in {"decimal", "numeric"}:
        return f"{data_type}({precision},{scale})"

    if data_type in {"datetime2", "datetimeoffset", "time"} and scale is not None:
        return f"{data_type}({scale})"

    return data_type


def add_edge(
    graph: nx.MultiDiGraph,
    source: str,
    target: str,
    relationship: str,
    **attrs: Any,
) -> None:
    edge_attrs = {"relationship": relationship, **compact_dict(attrs)}
    graph.add_edge(source, target, **edge_attrs)


def build_graph(
    tables: list[dict[str, Any]],
    columns: list[dict[str, Any]],
    primary_keys: list[dict[str, Any]],
    foreign_keys: list[dict[str, Any]],
    routines: list[dict[str, Any]],
    dependencies: list[dict[str, Any]],
    include_definitions: bool,
) -> nx.MultiDiGraph:
    nx = _require_networkx()
    graph = nx.MultiDiGraph(
        name="SQL Server Database Structure",
        directed=True,
        generated_at=datetime.now().astimezone().isoformat(),
    )

    pk_lookup: dict[tuple[str, str, str], dict[str, Any]] = {}
    for pk in primary_keys:
        key = (pk["schema_name"], pk["table_name"], pk["column_name"])
        pk_lookup[key] = pk

    fk_source_columns = {
        (fk["parent_schema"], fk["parent_table"], fk["parent_column"])
        for fk in foreign_keys
    }
    fk_target_columns = {
        (fk["referenced_schema"], fk["referenced_table"], fk["referenced_column"])
        for fk in foreign_keys
    }

    for table in tables:
        node_id = table_id(table["schema_name"], table["table_name"])
        graph.add_node(
            node_id,
            node_type="Table",
            name=table["table_name"],
            qualified_name=f'{table["schema_name"]}.{table["table_name"]}',
            schema=table["schema_name"],
            object_id=int(table["object_id"]),
            created_at=json_safe(table["create_date"]),
            modified_at=json_safe(table["modify_date"]),
            is_memory_optimized=bool(table["is_memory_optimized"]),
            temporal_type=str(table["temporal_type_desc"]),
        )

    for column in columns:
        key = (column["schema_name"], column["table_name"], column["column_name"])
        pk = pk_lookup.get(key)
        col_node_id = column_id(*key)
        parent_table_id = table_id(column["schema_name"], column["table_name"])

        graph.add_node(
            col_node_id,
            node_type="Column",
            name=column["column_name"],
            qualified_name=f'{column["schema_name"]}.{column["table_name"]}.{column["column_name"]}',
            schema=column["schema_name"],
            table=column["table_name"],
            table_object_id=int(column["table_object_id"]),
            column_id=int(column["column_id"]),
            data_type=column["data_type"],
            type_display=sql_type_display(column),
            max_length=column["max_length"],
            precision=int(column["precision"]),
            scale=int(column["scale"]),
            nullable=bool(column["is_nullable"]),
            identity=bool(column["is_identity"]),
            computed=bool(column["is_computed"]),
            default_definition=column["default_definition"],
            computed_definition=column["computed_definition"],
            collation=column["collation_name"],
            is_primary_key=pk is not None,
            primary_key_name=pk["constraint_name"] if pk else None,
            primary_key_ordinal=int(pk["key_ordinal"]) if pk else None,
            is_foreign_key=key in fk_source_columns,
            is_referenced_key=key in fk_target_columns,
        )

        add_edge(
            graph,
            parent_table_id,
            col_node_id,
            "Stores",
            ordinal=int(column["column_id"]),
        )

    for fk in foreign_keys:
        source = column_id(fk["parent_schema"], fk["parent_table"], fk["parent_column"])
        target = column_id(
            fk["referenced_schema"], fk["referenced_table"], fk["referenced_column"]
        )

        if source not in graph or target not in graph:
            continue

        add_edge(
            graph,
            source,
            target,
            "Links",
            foreign_key_name=fk["foreign_key_name"],
            constraint_column_id=int(fk["constraint_column_id"]),
            delete_action=fk["delete_referential_action_desc"],
            update_action=fk["update_referential_action_desc"],
            is_disabled=bool(fk["is_disabled"]),
            is_not_trusted=bool(fk["is_not_trusted"]),
        )

    routine_node_by_object_id: dict[int, str] = {}

    for routine in routines:
        code = routine["object_type_code"]
        if code in PROCEDURE_TYPES:
            node_type = "Procedure"
        elif code in FUNCTION_TYPES:
            node_type = "Function"
        else:
            continue

        definition = routine.get("definition") or ""
        definition_hash = (
            hashlib.sha256(definition.encode("utf-8")).hexdigest()
            if definition
            else None
        )

        node_id = routine_id(node_type, routine["schema_name"], routine["object_name"])
        routine_node_by_object_id[int(routine["object_id"])] = node_id

        attrs: dict[str, Any] = {
            "node_type": node_type,
            "name": routine["object_name"],
            "qualified_name": f'{routine["schema_name"]}.{routine["object_name"]}',
            "schema": routine["schema_name"],
            "object_id": int(routine["object_id"]),
            "sql_object_type": code,
            "sql_object_type_desc": routine["type_desc"],
            "created_at": json_safe(routine["create_date"]),
            "modified_at": json_safe(routine["modify_date"]),
            "is_schema_bound": bool(routine["is_schema_bound"])
            if routine["is_schema_bound"] is not None
            else False,
            "uses_ansi_nulls": bool(routine["uses_ansi_nulls"])
            if routine["uses_ansi_nulls"] is not None
            else False,
            "uses_quoted_identifier": bool(routine["uses_quoted_identifier"])
            if routine["uses_quoted_identifier"] is not None
            else False,
            "has_definition": bool(definition),
            "definition_sha256": definition_hash,
        }
        if include_definitions:
            attrs["definition"] = definition

        graph.add_node(node_id, **attrs)

    for dependency in dependencies:
        source = routine_node_by_object_id.get(int(dependency["referencing_object_id"]))
        if source is None:
            continue

        ref_schema = dependency["referenced_schema"]
        ref_table = dependency["referenced_table"]
        ref_column = dependency["referenced_column"]

        if not ref_schema or not ref_table:
            continue

        if ref_column:
            target = column_id(ref_schema, ref_table, ref_column)
            target_kind = "Column"
        else:
            target = table_id(ref_schema, ref_table)
            target_kind = "Table"

        if target not in graph:
            continue

        add_edge(
            graph,
            source,
            target,
            "Creates",
            target_kind=target_kind,
            dependency_source="sys.sql_expression_dependencies",
            is_schema_bound_reference=bool(dependency["is_schema_bound_reference"]),
            is_ambiguous=bool(dependency["is_ambiguous"]),
        )

    return graph


def graph_to_json(graph: nx.MultiDiGraph) -> dict[str, Any]:
    nodes = [
        {"id": node_id, **compact_dict(dict(attrs))}
        for node_id, attrs in graph.nodes(data=True)
    ]
    edges = [
        {
            "source": source,
            "target": target,
            "key": key,
            **compact_dict(dict(attrs)),
        }
        for source, target, key, attrs in graph.edges(keys=True, data=True)
    ]
    return {
        "metadata": compact_dict(dict(graph.graph)),
        "directed": True,
        "multigraph": True,
        "nodes": nodes,
        "edges": edges,
    }


def markdown_summary(graph: nx.MultiDiGraph) -> str:
    node_counts = Counter(
        attrs.get("node_type", "Unknown") for _, attrs in graph.nodes(data=True)
    )
    edge_counts = Counter(
        attrs.get("relationship", "Unknown")
        for _, _, attrs in graph.edges(data=True)
    )

    tables: dict[str, dict[str, Any]] = {}
    columns_by_table: dict[str, list[dict[str, Any]]] = defaultdict(list)
    routines: list[tuple[str, dict[str, Any]]] = []
    table_links: list[str] = []

    for node_id, attrs in graph.nodes(data=True):
        node_type = attrs.get("node_type")
        if node_type == "Table":
            tables[node_id] = dict(attrs)
        elif node_type == "Column":
            parent = table_id(attrs["schema"], attrs["table"])
            columns_by_table[parent].append(dict(attrs))
        elif node_type in {"Procedure", "Function"}:
            routines.append((node_id, dict(attrs)))

    for source, target, attrs in graph.edges(data=True):
        if attrs.get("relationship") == "Links":
            source_attrs = graph.nodes[source]
            target_attrs = graph.nodes[target]
            table_links.append(
                f"- `{source_attrs['schema']}.{source_attrs['table']}` "
                f"via `{source_attrs['name']}` → "
                f"`{target_attrs['schema']}.{target_attrs['table']}` "
                f"via `{target_attrs['name']}` "
                f"(`{attrs.get('foreign_key_name', '')}`)"
            )

    lines = [
        "# Database Structure Graph",
        "",
        f"Generated: `{graph.graph.get('generated_at', '')}`",
        "",
        "## Summary",
        "",
        "| Item | Count |",
        "|---|---:|",
    ]
    for node_type in ("Table", "Column", "Function", "Procedure"):
        lines.append(f"| {node_type} nodes | {node_counts[node_type]} |")
    for relationship in ("Stores", "Links", "Creates"):
        lines.append(f"| {relationship} relationships | {edge_counts[relationship]} |")

    lines.extend(["", "## Table Connections", ""])
    lines.extend(sorted(table_links) if table_links else ["No foreign keys found."])

    lines.extend(["", "## Tables and Columns", ""])
    for table_node_id in sorted(
        tables, key=lambda node: tables[node]["qualified_name"].lower()
    ):
        table = tables[table_node_id]
        lines.extend(
            [
                f"### {table['qualified_name']}",
                "",
                "| Column | Type | Nullable | Key information |",
                "|---|---|---:|---|",
            ]
        )

        table_columns = sorted(
            columns_by_table.get(table_node_id, []),
            key=lambda col: int(col["column_id"]),
        )
        for column in table_columns:
            key_info = []
            if column.get("is_primary_key"):
                key_info.append("PK")
            if column.get("is_foreign_key"):
                key_info.append("FK")
            if column.get("is_referenced_key"):
                key_info.append("Referenced key")
            if column.get("identity"):
                key_info.append("Identity")
            if column.get("computed"):
                key_info.append("Computed")

            lines.append(
                f"| `{column['name']}` | `{column['type_display']}` | "
                f"{'Yes' if column.get('nullable') else 'No'} | "
                f"{', '.join(key_info)} |"
            )
        lines.append("")

    lines.extend(["## Procedures and Functions", ""])
    if not routines:
        lines.append("No procedures or functions found.")
    else:
        for node_id, routine in sorted(
            routines, key=lambda item: item[1]["qualified_name"].lower()
        ):
            outgoing = [
                graph.nodes[target].get("qualified_name", target)
                for _, target, attrs in graph.out_edges(node_id, data=True)
                if attrs.get("relationship") == "Creates"
            ]
            lines.append(f"### {routine['node_type']}: {routine['qualified_name']}")
            lines.append("")
            lines.append(f"- SQL type: `{routine.get('sql_object_type_desc', '')}`")
            lines.append(f"- Modified: `{routine.get('modified_at', '')}`")
            lines.append(f"- Referenced tables/columns: {len(outgoing)}")
            for target in sorted(set(outgoing)):
                lines.append(f"  - `{target}`")
            lines.append("")

    lines.extend(
        [
            "## Relationship Semantics",
            "",
            "- `Stores`: Table → Column",
            "- `Links`: Foreign-key Column → Referenced key Column",
            (
                "- `Creates`: Procedure/Function → referenced Table or Column. "
                "This label represents a catalog dependency, not guaranteed DDL creation."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def atomic_write_text(path: Path, content: str) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)


def write_outputs(graph: nx.MultiDiGraph, output_dir: Path) -> None:
    nx = _require_networkx()
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "db_graph.json"
    graphml_path = output_dir / "db_graph.graphml"
    markdown_path = output_dir / "db_graph.md"

    json_content = json.dumps(
        graph_to_json(graph), indent=2, sort_keys=False, ensure_ascii=False
    )
    atomic_write_text(json_path, json_content)
    atomic_write_text(markdown_path, markdown_summary(graph))

    graphml_graph = nx.MultiDiGraph()

    graphml_graph.graph.update(
        {
            key: graphml_safe(value)
            for key, value in graph.graph.items()
        }
    )

    for node_id, attrs in graph.nodes(data=True):
        graphml_graph.add_node(
            node_id,
            **{
                key: graphml_safe(value)
                for key, value in attrs.items()
            },
        )

    for edge_number, (source, target, key, attrs) in enumerate(
        graph.edges(keys=True, data=True)
    ):
        edge_uid = f"edge-{edge_number}"

        edge_attrs = {
            name: graphml_safe(value)
            for name, value in attrs.items()
        }

        edge_attrs["edge_uid"] = edge_uid
        edge_attrs["networkx_key"] = str(key)

        graphml_graph.add_edge(
            source,
            target,
            key=edge_uid,
            **edge_attrs,
        )

    temp_graphml = graphml_path.with_suffix(".graphml.tmp")

    nx.write_graphml(
        graphml_graph,
        temp_graphml,
        encoding="utf-8",
        edge_id_from_attribute="edge_uid",
    )

    temp_graphml.replace(graphml_path)


def fetch_schema(cursor: "pyodbc.Cursor") -> dict[str, list[dict[str, Any]]]:
    """Run the six sys.* catalog queries against an open cursor.

    Returns a dict keyed by the six row-set names consumed by build_graph().
    Pure I/O — keeps the network/DB access in one place so callers can stub it.
    """
    return {
        "tables": fetch_rows(cursor, TABLES_SQL),
        "columns": fetch_rows(cursor, COLUMNS_SQL),
        "primary_keys": fetch_rows(cursor, PRIMARY_KEYS_SQL),
        "foreign_keys": fetch_rows(cursor, FOREIGN_KEYS_SQL),
        "routines": fetch_rows(cursor, ROUTINES_SQL),
        "dependencies": fetch_rows(cursor, DEPENDENCIES_SQL),
    }


def build_graph_data(
    connection: "pyodbc.Connection",
    include_definitions: bool = False,
) -> dict[str, Any]:
    """Importable core: read the schema over `connection` and return the JSON dict.

    This is the in-process entry point used by graph_io's pooled-connection build
    path.  It performs exactly the same reads + NetworkX construction +
    serialization as the CLI, so the returned dict is byte-/shape-identical to
    what graph_to_json() produces for the subprocess build.  It does NOT touch the
    filesystem and does NOT build the pyvis HTML — callers that want files use
    build_and_write().

    `connection` is any object exposing pyodbc's `.cursor()` (a live pyodbc
    connection in production, a stub in tests).
    """
    cursor = connection.cursor()
    rows = fetch_schema(cursor)
    graph = build_graph(
        tables=rows["tables"],
        columns=rows["columns"],
        primary_keys=rows["primary_keys"],
        foreign_keys=rows["foreign_keys"],
        routines=rows["routines"],
        dependencies=rows["dependencies"],
        include_definitions=include_definitions,
    )
    return graph_to_json(graph)


# ---------------------------------------------------------------------------
# Targeted (bounded-neighborhood) build core
# ---------------------------------------------------------------------------


def _split_entry_point(entry_point: str, entry_type: str) -> tuple[str, str, "str | None"]:
    """Split a qualified entry identifier into (schema, object_name, column_name).

    Accepted forms:
        - table / function / procedure: ``schema.object``
        - column:                       ``schema.table.column``

    The unqualified case (``object`` with no schema) is rejected: SQL Server
    object identity is schema-scoped, so a bare name is ambiguous.  Raises
    ValueError on a malformed identifier so the caller can surface a clear error.
    """
    if not entry_point or not entry_point.strip():
        raise ValueError("entry_point must be a non-empty object identifier")

    parts = entry_point.split(".")
    if entry_type == "column":
        if len(parts) != 3:
            raise ValueError(
                f"column entry_point must be 'schema.table.column', got {entry_point!r}"
            )
        schema, table, column = parts
        return schema, table, column

    # table / function / procedure
    if len(parts) != 2:
        raise ValueError(
            f"{entry_type} entry_point must be 'schema.object', got {entry_point!r}"
        )
    schema, name = parts
    return schema, name, None


def _in_clause(count: int) -> str:
    """Return a comma-separated run of ``count`` pyodbc parameter markers."""
    return ", ".join("?" for _ in range(count)) if count else "NULL"


def _fetch_rows_in(
    cursor: "pyodbc.Cursor",
    sql_template: str,
    object_ids: "list[int]",
    repeats: int = 1,
) -> list[dict[str, Any]]:
    """Run an ``IN (...)``-filtered query for a frontier of object_ids.

    ``sql_template`` contains one or more ``{placeholders}`` slots; each is
    expanded to a parameter run matching ``object_ids`` and the id list is bound
    ``repeats`` times (queries that filter on two columns — e.g. FK parent OR
    referenced — embed the placeholder twice and pass repeats=2).  An empty
    frontier yields no rows without touching the database.
    """
    if not object_ids:
        return []
    placeholders = _in_clause(len(object_ids))
    sql = sql_template.format(placeholders=placeholders)
    # Derive the bind-repeat count from the template itself (source of truth) so
    # a future edit that adds a {placeholders} slot can't silently misbind params.
    repeats = sql_template.count("{placeholders}") or repeats
    params = list(object_ids) * repeats
    cursor.execute(sql, *params)
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _resolve_entry_object_id(
    cursor: "pyodbc.Cursor",
    schema: str,
    name: str,
    entry_type: str,
) -> "int | None":
    """Resolve a table/routine name to its object_id (column uses its table).

    Returns None when the object does not exist in the (non-shipped) catalog, so
    the caller can return an empty-but-well-formed graph rather than raising.
    """
    if entry_type in ("table", "column"):
        cursor.execute(RESOLVE_TABLE_OBJECT_ID_SQL, schema, name)
    else:  # function / procedure
        cursor.execute(RESOLVE_ROUTINE_OBJECT_ID_SQL, schema, name)
    row = cursor.fetchone()
    return int(row[0]) if row else None


def _discover_object_ids(
    cursor: "pyodbc.Cursor",
    seed_object_id: int,
    max_depth: int,
) -> set[int]:
    """Bounded BFS from the seed object_id over FK + dependency edges.

    Returns the set of object_ids within ``max_depth`` hops of the seed
    (inclusive of the seed).  Each hop issues WHERE-filtered FK and dependency
    queries against the CURRENT frontier only, so the database — not Python —
    does the pruning.  Edges are followed in both directions:

      * FK:         parent table  <-> referenced table
      * dependency: referencing routine <-> referenced table

    so a depth-1 neighborhood of a table includes the tables it references, the
    tables that reference it, and the routines that read it (and vice versa).
    ``max_depth <= 0`` returns just the seed.
    """
    discovered: set[int] = {seed_object_id}
    frontier: set[int] = {seed_object_id}

    for _ in range(max(0, max_depth)):
        if not frontier:
            break
        frontier_ids = sorted(frontier)

        neighbors: set[int] = set()

        # FK edges touching the frontier (parent OR referenced side).
        for fk in _fetch_rows_in(
            cursor, FOREIGN_KEYS_FILTERED_SQL, frontier_ids, repeats=2
        ):
            neighbors.add(int(fk["parent_object_id"]))
            neighbors.add(int(fk["referenced_object_id"]))

        # Dependency edges touching the frontier (referencing routine OR
        # referenced table side).
        for dep in _fetch_rows_in(
            cursor, DEPENDENCIES_FILTERED_SQL, frontier_ids, repeats=2
        ):
            neighbors.add(int(dep["referencing_object_id"]))
            ref_object_id = dep.get("referenced_object_id")
            if ref_object_id is not None:
                neighbors.add(int(ref_object_id))

        new_ids = neighbors - discovered
        discovered |= new_ids
        frontier = new_ids

    return discovered


def build_targeted_graph_data(
    connection: "pyodbc.Connection",
    entry_point: str,
    entry_type: str,
    max_depth: int = 1,
    include_definitions: bool = False,
) -> dict[str, Any]:
    """Importable core: build a BOUNDED neighborhood graph and return the dict.

    Starting from ``entry_point`` (resolved via ``entry_type``), expand outward
    up to ``max_depth`` hops over FK and routine-dependency edges, fetching only
    the rows for the discovered object set via WHERE-filtered catalog queries.
    The discovered rows feed the UNCHANGED build_graph() + graph_to_json(), so
    the result is byte-/shape-identical to the full build — just a subset.

    Semantics:
        * ``max_depth == 0`` -> the entry object only (plus its own columns when
          the entry is a table; a column entry still includes its parent table's
          columns so the column node and its Stores edge are present).
        * A missing entry object yields an empty-but-well-formed graph (no
          nodes/edges) rather than an error.

    Does NOT touch the filesystem, the TTL cache, or db_graph.json — callers that
    want the shared full-graph file use build_and_write().  ``connection`` is any
    object exposing pyodbc's ``.cursor()`` (live connection or test stub).
    """
    if entry_type not in TARGETED_ENTRY_TYPES:
        raise ValueError(
            f"entry_type must be one of {TARGETED_ENTRY_TYPES}, got {entry_type!r}"
        )

    schema, name, _column = _split_entry_point(entry_point, entry_type)

    cursor = connection.cursor()
    seed_object_id = _resolve_entry_object_id(cursor, schema, name, entry_type)

    if seed_object_id is None:
        # Unknown entry object: return an empty graph with the same shape.
        empty = build_graph(
            tables=[],
            columns=[],
            primary_keys=[],
            foreign_keys=[],
            routines=[],
            dependencies=[],
            include_definitions=include_definitions,
        )
        return graph_to_json(empty)

    object_ids = sorted(_discover_object_ids(cursor, seed_object_id, max_depth))

    # Assemble the final row lists over the FULL discovered set with one filtered
    # query each — the build then runs over exactly this neighborhood.
    tables = _fetch_rows_in(cursor, TABLES_FILTERED_SQL, object_ids)
    columns = _fetch_rows_in(cursor, COLUMNS_FILTERED_SQL, object_ids)
    primary_keys = _fetch_rows_in(cursor, PRIMARY_KEYS_FILTERED_SQL, object_ids)
    foreign_keys = _fetch_rows_in(
        cursor, FOREIGN_KEYS_FILTERED_SQL, object_ids, repeats=2
    )
    routines = _fetch_rows_in(cursor, ROUTINES_FILTERED_SQL, object_ids)
    dependencies = _fetch_rows_in(
        cursor, DEPENDENCIES_FILTERED_SQL, object_ids, repeats=2
    )

    graph = build_graph(
        tables=tables,
        columns=columns,
        primary_keys=primary_keys,
        foreign_keys=foreign_keys,
        routines=routines,
        dependencies=dependencies,
        include_definitions=include_definitions,
    )
    return graph_to_json(graph)


def build_and_write(
    connection: "pyodbc.Connection",
    output_dir: Path,
    include_definitions: bool = False,
    include_html: bool = False,
) -> nx.MultiDiGraph:
    """Importable core: read the schema and write db_graph.{json,md,graphml}.

    Returns the built NetworkX graph so callers can derive summaries (the CLI
    prints node/edge counts).  `include_html` is accepted for API symmetry with
    the data-only tool path; HTML generation lives in build_graph_html.py and is
    intentionally not done here, so this path never imports pyvis.  The Flask UI
    builds the HTML via its own step.
    """
    cursor = connection.cursor()
    rows = fetch_schema(cursor)
    graph = build_graph(
        tables=rows["tables"],
        columns=rows["columns"],
        primary_keys=rows["primary_keys"],
        foreign_keys=rows["foreign_keys"],
        routines=rows["routines"],
        dependencies=rows["dependencies"],
        include_definitions=include_definitions,
    )
    write_outputs(graph, Path(output_dir))
    return graph


def main() -> int:
    args = parse_args()

    if not args.connection_string:
        print(
            "Error: provide --connection-string or set DB_CONNECTION_STRING.",
            file=sys.stderr,
        )
        return 2

    if args.entry_point and not args.entry_type:
        print(
            "Error: --entry-type is required when --entry-point is given.",
            file=sys.stderr,
        )
        return 2

    output_dir = Path(args.output_dir).expanduser().resolve()

    pyodbc = _require_pyodbc()

    try:
        connection = pyodbc.connect(
            args.connection_string,
            timeout=args.timeout,
            autocommit=True,
        )
    except pyodbc.Error as exc:
        print(f"Database connection failed: {exc}", file=sys.stderr)
        return 1

    # Targeted (bounded-neighborhood) CLI build: write the scoped JSON only.
    # The default (no --entry-point) full build below is unchanged.
    if args.entry_point:
        try:
            data = build_targeted_graph_data(
                connection,
                entry_point=args.entry_point,
                entry_type=args.entry_type,
                max_depth=args.max_depth,
                include_definitions=args.include_definitions,
            )
        except pyodbc.Error as exc:
            print(f"Schema extraction failed: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"Invalid entry point: {exc}", file=sys.stderr)
            return 2
        finally:
            connection.close()

        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / "db_graph.json"
        atomic_write_text(
            json_path,
            json.dumps(data, indent=2, sort_keys=False, ensure_ascii=False),
        )

        node_counts = Counter(n.get("node_type", "Unknown") for n in data["nodes"])
        edge_counts = Counter(e.get("relationship", "Unknown") for e in data["edges"])
        print("Targeted database graph built successfully.")
        print(
            f"Entry point: {args.entry_point} ({args.entry_type}), "
            f"max depth: {args.max_depth}"
        )
        print(f"Output file: {json_path}")
        print(
            "Nodes: "
            + ", ".join(
                f"{kind}={node_counts[kind]}"
                for kind in ("Table", "Column", "Function", "Procedure")
            )
        )
        print(
            "Relationships: "
            + ", ".join(
                f"{kind}={edge_counts[kind]}"
                for kind in ("Stores", "Links", "Creates")
            )
        )
        return 0

    try:
        graph = build_and_write(
            connection,
            output_dir,
            include_definitions=args.include_definitions,
        )
    except pyodbc.Error as exc:
        print(f"Schema extraction failed: {exc}", file=sys.stderr)
        return 1
    finally:
        connection.close()

    node_counts = Counter(
        attrs.get("node_type", "Unknown") for _, attrs in graph.nodes(data=True)
    )
    edge_counts = Counter(
        attrs.get("relationship", "Unknown")
        for _, _, attrs in graph.edges(data=True)
    )

    print("Database graph rebuilt successfully.")
    print(f"Output directory: {output_dir}")
    print(
        "Nodes: "
        + ", ".join(
            f"{kind}={node_counts[kind]}"
            for kind in ("Table", "Column", "Function", "Procedure")
        )
    )
    print(
        "Relationships: "
        + ", ".join(
            f"{kind}={edge_counts[kind]}"
            for kind in ("Stores", "Links", "Creates")
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
