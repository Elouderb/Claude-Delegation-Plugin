"""
Database graph MCP tool implementations (tools 8-13).

Two build strategies are used, by tool:

  * TARGETED (in-memory, bounded neighborhood) — db_get_table, db_get_column,
    db_get_table_relationships, db_get_routine_dependencies.  These pass their
    queried object as the entry point to build_targeted_database_graph() with a
    sensible default depth and operate on the returned scoped dict.  They do NOT
    write db_graph.json and do NOT use the TTL-cached full graph.

  * FULL (TTL-cached file) — db_search_schema, db_find_relationship_path.  A
    global search and an arbitrary two-endpoint path both need every object, so
    these keep the unchanged refresh_database_graph() + load_database_graph()
    flow against the shared, TTL-cached db_graph.json.

The targeted entry-point default depths come from AGENT_OS_DB_GRAPH_DEPTH (a
global override) with per-tool fallback constants below.

Functions are imported by server.py and registered with @server.tool().
"""

import os
from collections import defaultdict
from typing import Optional

from graph_io import (
    build_targeted_database_graph,
    format_graph_response,
    load_database_graph,
    log,
    refresh_database_graph,
)

# Per-tool default neighborhood depths for the targeted builds.  A single object
# lookup needs only its immediate neighbors (depth 1); a routine's dependency
# fan-out is one hop further to reach the columns of the tables it touches
# (routine -> table -> columns), so it defaults to depth 2.
_DEFAULT_TABLE_DEPTH = 1
_DEFAULT_COLUMN_DEPTH = 1
_DEFAULT_RELATIONSHIPS_DEPTH = 1
_DEFAULT_ROUTINE_DEPTH = 2


def _targeted_depth(default: int) -> int:
    """Resolve a targeted-build depth: AGENT_OS_DB_GRAPH_DEPTH or the fallback.

    AGENT_OS_DB_GRAPH_DEPTH, when set to a non-negative integer, overrides every
    targeted tool's default depth (handy for widening the neighborhood without a
    code change).  A blank, malformed, or negative value falls back to the
    per-tool ``default``.
    """
    raw = os.environ.get("AGENT_OS_DB_GRAPH_DEPTH", "")
    if raw.strip() == "":
        return default
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return default
    return value if value >= 0 else default


def _strip_type_prefix(node_id) -> str:
    """Normalize a build-emitted node id to its bare ``schema.name``.

    The build keys nodes as ``"<type>:<schema>.<name>"`` — e.g. ``table:dbo.Order``,
    ``column:dbo.Order.Id``, ``procedure:dbo.MyProc`` (see build_db_graph.table_id /
    column_id / routine_id). The tools receive the raw ``schema.name`` from the
    caller, so strip the ``<type>:`` prefix before comparing. A no-op on ids that
    have no prefix.
    """
    if not isinstance(node_id, str):
        return ""
    return node_id.split(":", 1)[1] if ":" in node_id else node_id


def db_get_table(table_name: str) -> dict:
    """Get table metadata, columns, keys, and relationships.

    TARGETED build: scopes to the bounded neighborhood around ``table_name``
    (default depth 1) instead of loading the whole-schema graph.
    """
    try:
        graph_data = build_targeted_database_graph(
            table_name, "table", _targeted_depth(_DEFAULT_TABLE_DEPTH)
        )
        if not graph_data:
            return format_graph_response("database", {"table": table_name}, {},
                                        ["Database graph not found"])

        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])

        table_node = None
        for node in nodes:
            if _strip_type_prefix(node.get("id")) == table_name and node.get("type") == "Table":
                table_node = node
                break

        if not table_node:
            return format_graph_response("database", {"table": table_name}, {},
                                        [f"Table {table_name} not found"])

        # Find related columns and relationships
        columns = []
        incoming_refs = []
        outgoing_refs = []

        for edge in edges:
            if _strip_type_prefix(edge.get("source")) == table_name:
                outgoing_refs.append({
                    "target": _strip_type_prefix(edge.get("target")),
                    "relationship": edge.get("relationship")
                })
            elif _strip_type_prefix(edge.get("target")) == table_name:
                incoming_refs.append({
                    "source": _strip_type_prefix(edge.get("source")),
                    "relationship": edge.get("relationship")
                })

        return format_graph_response("database", {"table": table_name}, {
            "table": table_node,
            "incoming_references": incoming_refs,
            "outgoing_references": outgoing_refs
        })
    except Exception as e:
        log(f"ERROR in db_get_table: {e}")
        return format_graph_response("database", {"table": table_name}, {}, [str(e)])


def db_get_column(table_name: str, column_name: str) -> dict:
    """Get column metadata, type, constraints, and dependencies.

    TARGETED build: scopes to the bounded neighborhood around the column
    ``table_name.column_name`` (default depth 1) instead of the whole schema.
    """
    try:
        entry_point = f"{table_name}.{column_name}"
        graph_data = build_targeted_database_graph(
            entry_point, "column", _targeted_depth(_DEFAULT_COLUMN_DEPTH)
        )
        if not graph_data:
            return format_graph_response("database", {"table": table_name, "column": column_name},
                                        {}, ["Database graph not found"])

        nodes = graph_data.get("nodes", [])
        col_id = f"{table_name}.{column_name}"

        col_node = None
        for node in nodes:
            if _strip_type_prefix(node.get("id")) == col_id and node.get("type") == "Column":
                col_node = node
                break

        if not col_node:
            return format_graph_response("database", {"table": table_name, "column": column_name},
                                        {}, [f"Column {col_id} not found"])

        return format_graph_response("database", {"table": table_name, "column": column_name},
                                    {"column": col_node})
    except Exception as e:
        log(f"ERROR in db_get_column: {e}")
        return format_graph_response("database", {"table": table_name, "column": column_name},
                                    {}, [str(e)])


def db_search_schema(query: str, object_type: Optional[str] = None) -> dict:
    """Search Table, Column, Function, and Procedure nodes."""
    try:
        success, error = refresh_database_graph()
        if not success:
            return format_graph_response("database", {"query": query, "type": object_type},
                                        {}, [f"Graph refresh failed: {error}"])

        graph_data = load_database_graph()
        if not graph_data:
            return format_graph_response("database", {"query": query}, {},
                                        ["Database graph not found"])

        nodes = graph_data.get("nodes", [])
        results = []

        for node in nodes:
            node_type = node.get("type", "")
            if object_type and object_type != node_type:
                continue

            if query.lower() in node.get("id", "").lower() or \
               query.lower() in node.get("label", "").lower():
                results.append({
                    "id": node.get("id"),
                    "label": node.get("label"),
                    "type": node_type
                })

        return format_graph_response("database", {"query": query, "type": object_type},
                                    {"results": results})
    except Exception as e:
        log(f"ERROR in db_search_schema: {e}")
        return format_graph_response("database", {"query": query}, {}, [str(e)])


def db_get_table_relationships(table_name: str) -> dict:
    """Get table-level incoming and outgoing relationships with exact key-column pairs.

    TARGETED build: scopes to the bounded neighborhood around ``table_name``
    (default depth 1) instead of loading the whole-schema graph.
    """
    try:
        graph_data = build_targeted_database_graph(
            table_name, "table", _targeted_depth(_DEFAULT_RELATIONSHIPS_DEPTH)
        )
        if not graph_data:
            return format_graph_response("database", {"table": table_name}, {},
                                        ["Database graph not found"])

        edges = graph_data.get("edges", [])
        incoming = []
        outgoing = []

        for edge in edges:
            if _strip_type_prefix(edge.get("source")) == table_name:
                outgoing.append({
                    "target": _strip_type_prefix(edge.get("target")),
                    "relationship": edge.get("relationship"),
                    "metadata": edge.get("metadata", {})
                })
            elif _strip_type_prefix(edge.get("target")) == table_name:
                incoming.append({
                    "source": _strip_type_prefix(edge.get("source")),
                    "relationship": edge.get("relationship"),
                    "metadata": edge.get("metadata", {})
                })

        return format_graph_response("database", {"table": table_name}, {
            "incoming": incoming,
            "outgoing": outgoing
        })
    except Exception as e:
        log(f"ERROR in db_get_table_relationships: {e}")
        return format_graph_response("database", {"table": table_name}, {}, [str(e)])


def db_find_relationship_path(table1: str, table2: str) -> dict:
    """Find foreign-key paths between two tables."""
    try:
        success, error = refresh_database_graph()
        if not success:
            return format_graph_response("database", {"from": table1, "to": table2},
                                        {}, [f"Graph refresh failed: {error}"])

        graph_data = load_database_graph()
        if not graph_data:
            return format_graph_response("database", {"from": table1, "to": table2},
                                        {}, ["Database graph not found"])

        # Use graph_find_path with database graph to find relationship paths
        edges = graph_data.get("edges", [])
        adj = defaultdict(list)

        for edge in edges:
            src, tgt = edge.get("source"), edge.get("target")
            adj[src].append((tgt, edge.get("relationship"), edge.get("metadata", {})))
            adj[tgt].append((src, edge.get("relationship"), edge.get("metadata", {})))

        # BFS over prefixed node ids — the graph keys edges by "table:schema.name"
        # / "column:...". Seed and goal use the table: prefix; strip it in output.
        start, goal = f"table:{_strip_type_prefix(table1)}", f"table:{_strip_type_prefix(table2)}"
        queue = [(start, [start], [])]
        paths = []

        while queue and len(paths) < 5:
            node, path, rels = queue.pop(0)

            if len(path) > 10:
                continue

            if node == goal:
                paths.append({"path": [_strip_type_prefix(p) for p in path], "relationships": rels})
                continue

            for neighbor, rel, meta in adj.get(node, []):
                if neighbor not in path:
                    queue.append((neighbor, path + [neighbor], rels + [rel]))

        return format_graph_response("database", {"from": table1, "to": table2},
                                    {"paths": paths})
    except Exception as e:
        log(f"ERROR in db_find_relationship_path: {e}")
        return format_graph_response("database", {"from": table1, "to": table2},
                                    {}, [str(e)])


def db_get_routine_dependencies(routine_name: str) -> dict:
    """Get tables and columns connected to a function or procedure.

    TARGETED build: scopes to the bounded neighborhood around ``routine_name``
    (default depth 2 — one hop to the referenced tables, a second to reach their
    columns) instead of loading the whole-schema graph.  The entry type is given
    as ``procedure``; routine resolution is type-agnostic (it matches every
    function/procedure type), so a function entry resolves identically.
    """
    try:
        graph_data = build_targeted_database_graph(
            routine_name, "procedure", _targeted_depth(_DEFAULT_ROUTINE_DEPTH)
        )
        if not graph_data:
            return format_graph_response("database", {"routine": routine_name},
                                        {}, ["Database graph not found"])

        edges = graph_data.get("edges", [])
        dependencies = {"tables": [], "columns": []}

        for edge in edges:
            if _strip_type_prefix(edge.get("source")) == routine_name and edge.get("relationship") == "Creates":
                target = edge.get("target") or ""
                bucket = "columns" if target.startswith("column:") else "tables"
                dependencies[bucket].append(_strip_type_prefix(target))
            elif _strip_type_prefix(edge.get("target")) == routine_name and edge.get("relationship") == "Uses":
                source = edge.get("source") or ""
                bucket = "columns" if source.startswith("column:") else "tables"
                dependencies[bucket].append(_strip_type_prefix(source))

        return format_graph_response("database", {"routine": routine_name},
                                    {"dependencies": dependencies})
    except Exception as e:
        log(f"ERROR in db_get_routine_dependencies: {e}")
        return format_graph_response("database", {"routine": routine_name},
                                    {}, [str(e)])
