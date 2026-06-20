"""
Database graph MCP tool implementations (tools 8-13).

All tools require refresh_database_graph() to succeed before querying.
Functions are imported by server.py and registered with @server.tool().
"""

from collections import defaultdict
from typing import Optional

from graph_io import (
    format_graph_response,
    load_database_graph,
    log,
    refresh_database_graph,
)


def db_get_table(table_name: str) -> dict:
    """Get table metadata, columns, keys, and relationships."""
    try:
        success, error = refresh_database_graph()
        if not success:
            return format_graph_response("database", {"table": table_name}, {},
                                        [f"Graph refresh failed: {error}"])

        graph_data = load_database_graph()
        if not graph_data:
            return format_graph_response("database", {"table": table_name}, {},
                                        ["Database graph not found"])

        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])

        table_node = None
        for node in nodes:
            if node.get("id") == table_name and node.get("type") == "Table":
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
            if edge.get("source") == table_name:
                outgoing_refs.append({
                    "target": edge.get("target"),
                    "relationship": edge.get("relationship")
                })
            elif edge.get("target") == table_name:
                incoming_refs.append({
                    "source": edge.get("source"),
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
    """Get column metadata, type, constraints, and dependencies."""
    try:
        success, error = refresh_database_graph()
        if not success:
            return format_graph_response("database", {"table": table_name, "column": column_name},
                                        {}, [f"Graph refresh failed: {error}"])

        graph_data = load_database_graph()
        if not graph_data:
            return format_graph_response("database", {"table": table_name, "column": column_name},
                                        {}, ["Database graph not found"])

        nodes = graph_data.get("nodes", [])
        col_id = f"{table_name}.{column_name}"

        col_node = None
        for node in nodes:
            if node.get("id") == col_id and node.get("type") == "Column":
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
    """Get table-level incoming and outgoing relationships with exact key-column pairs."""
    try:
        success, error = refresh_database_graph()
        if not success:
            return format_graph_response("database", {"table": table_name}, {},
                                        [f"Graph refresh failed: {error}"])

        graph_data = load_database_graph()
        if not graph_data:
            return format_graph_response("database", {"table": table_name}, {},
                                        ["Database graph not found"])

        edges = graph_data.get("edges", [])
        incoming = []
        outgoing = []

        for edge in edges:
            if edge.get("source") == table_name:
                outgoing.append({
                    "target": edge.get("target"),
                    "relationship": edge.get("relationship"),
                    "metadata": edge.get("metadata", {})
                })
            elif edge.get("target") == table_name:
                incoming.append({
                    "source": edge.get("source"),
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

        # BFS
        queue = [(table1, [table1], [])]
        paths = []

        while queue and len(paths) < 5:
            node, path, rels = queue.pop(0)

            if len(path) > 10:
                continue

            if node == table2:
                paths.append({"path": path, "relationships": rels})
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
    """Get tables and columns connected to a function or procedure."""
    try:
        success, error = refresh_database_graph()
        if not success:
            return format_graph_response("database", {"routine": routine_name},
                                        {}, [f"Graph refresh failed: {error}"])

        graph_data = load_database_graph()
        if not graph_data:
            return format_graph_response("database", {"routine": routine_name},
                                        {}, ["Database graph not found"])

        edges = graph_data.get("edges", [])
        dependencies = {"tables": [], "columns": []}

        for edge in edges:
            if edge.get("source") == routine_name and edge.get("relationship") == "Creates":
                target = edge.get("target")
                if "." in target:
                    dependencies["columns"].append(target)
                else:
                    dependencies["tables"].append(target)
            elif edge.get("target") == routine_name and edge.get("relationship") == "Uses":
                source = edge.get("source")
                if "." in source:
                    dependencies["columns"].append(source)
                else:
                    dependencies["tables"].append(source)

        return format_graph_response("database", {"routine": routine_name},
                                    {"dependencies": dependencies})
    except Exception as e:
        log(f"ERROR in db_get_routine_dependencies: {e}")
        return format_graph_response("database", {"routine": routine_name},
                                    {}, [str(e)])
