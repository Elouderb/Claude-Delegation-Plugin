"""
Shared graph MCP tool implementations (tools 1-7).

Covers both the 'code' and 'database' graphs via a ``graph`` parameter.
Functions are imported by server.py and registered with @server.tool().
"""

import os
import subprocess
from collections import defaultdict
from typing import List, Optional

import graph_io
from graph_io import (
    format_graph_response,
    log,
    refresh_database_graph,
    type_filter_warning,
)


def graph_search_nodes(query: str, graph: str = "code", node_type: Optional[str] = None,
                       fuzzy: bool = False, limit: int = 50) -> dict:
    """Search nodes by name, qualified name, type, and metadata."""
    try:
        if graph == "database":
            success, error = refresh_database_graph()
            if not success:
                return format_graph_response("database", {"query": query}, {},
                                            [f"Graph refresh failed: {error}"])
            graph_data = graph_io.load_database_graph()
        else:
            graph_data = graph_io.load_code_graph()

        if not graph_data:
            return format_graph_response(graph, {"query": query}, {},
                                        ["Graph not found or empty"])

        results = []
        nodes = graph_data.get("nodes", [])
        present_types: set = set()

        for node in nodes:
            node_id = node.get("id", "")
            label = node.get("label", "")
            node_types = node.get("type", "")
            # Record every type present in the SAME pass, so the empty-filter
            # diagnostic below needs no second O(N) scan of the node list.
            if node_types not in (None, ""):
                present_types.add(str(node_types))

            # Apply type filter
            if node_type and node_type not in str(node_types):
                continue

            # Check if query matches
            matches = False
            if fuzzy:
                matches = query.lower() in node_id.lower() or query.lower() in label.lower()
            else:
                matches = query == node_id or query == label

            if matches:
                results.append({
                    "id": node_id,
                    "label": label,
                    "type": node_types,
                    "metadata": {k: v for k, v in node.items()
                               if k not in ["id", "label", "type"]}
                })

            if len(results) >= limit:
                break

        # A node_type filter that matches nothing is indistinguishable from "no
        # such symbol" (WINDOWS.md appendix). Warn ONLY when the requested type is
        # absent from the graph — a real type that simply did not match the query
        # is a genuine miss, not a filter error. present_types was gathered in the
        # loop above, so this is not a second O(N) pass.
        warnings = type_filter_warning(node_type, present_types) if not results else []

        truncated = len(results) >= limit
        return format_graph_response(graph, {"query": query, "type": node_type},
                                    {"nodes": results}, warnings=warnings,
                                    truncated=truncated)
    except Exception as e:
        log(f"ERROR in graph_search_nodes: {e}")
        return format_graph_response(graph, {"query": query}, {}, [str(e)])


def graph_get_node(node_id: str, graph: str = "code") -> dict:
    """Get complete metadata for one node."""
    try:
        if graph == "database":
            success, error = refresh_database_graph()
            if not success:
                return format_graph_response("database", {"node_id": node_id}, {},
                                            [f"Graph refresh failed: {error}"])
            graph_data = graph_io.load_database_graph()
        else:
            graph_data = graph_io.load_code_graph()

        if not graph_data:
            return format_graph_response(graph, {"node_id": node_id}, {},
                                        ["Graph not found or empty"])

        nodes = graph_data.get("nodes", [])
        for node in nodes:
            if node.get("id") == node_id:
                return format_graph_response(graph, {"node_id": node_id}, {"node": node})

        return format_graph_response(graph, {"node_id": node_id}, {},
                                    [f"Node {node_id} not found"])
    except Exception as e:
        log(f"ERROR in graph_get_node: {e}")
        return format_graph_response(graph, {"node_id": node_id}, {}, [str(e)])


def graph_get_neighbors(node_id: str, graph: str = "code", direction: str = "both",
                        relationship: Optional[str] = None) -> dict:
    """Get incoming, outgoing, or bidirectional 1-hop neighbors.

    Always returns DIRECT (single-hop) neighbors. Multi-hop expansion is
    graph_get_subgraph's job (it takes a ``depth``). A former ``depth`` parameter
    here was never read — the traversal returned 1-hop regardless — so it was
    removed rather than left as a silently-ignored no-op that promised more.
    """
    try:
        if graph == "database":
            success, error = refresh_database_graph()
            if not success:
                return format_graph_response("database", {"node_id": node_id}, {},
                                            [f"Graph refresh failed: {error}"])
            graph_data = graph_io.load_database_graph()
        else:
            graph_data = graph_io.load_code_graph()

        if not graph_data:
            return format_graph_response(graph, {"node_id": node_id}, {},
                                        ["Graph not found or empty"])

        edges = graph_data.get("edges", [])
        neighbors = {"incoming": [], "outgoing": []}

        for edge in edges:
            edge_rel = edge.get("relationship", "")
            if relationship and relationship != edge_rel:
                continue

            if direction in ["outgoing", "both"] and edge.get("source") == node_id:
                neighbors["outgoing"].append({
                    "target": edge.get("target"),
                    "relationship": edge_rel,
                    "confidence": edge.get("confidence")
                })

            if direction in ["incoming", "both"] and edge.get("target") == node_id:
                neighbors["incoming"].append({
                    "source": edge.get("source"),
                    "relationship": edge_rel,
                    "confidence": edge.get("confidence")
                })

        return format_graph_response(graph, {"node_id": node_id, "direction": direction},
                                    neighbors)
    except Exception as e:
        log(f"ERROR in graph_get_neighbors: {e}")
        return format_graph_response(graph, {"node_id": node_id}, {}, [str(e)])


def graph_find_path(source: str, target: str, graph: str = "code",
                    max_depth: int = 5, directed: bool = True) -> dict:
    """Find paths between nodes with maximum depth."""
    try:
        if graph == "database":
            success, error = refresh_database_graph()
            if not success:
                return format_graph_response("database", {"source": source, "target": target},
                                            {}, [f"Graph refresh failed: {error}"])
            graph_data = graph_io.load_database_graph()
        else:
            graph_data = graph_io.load_code_graph()

        if not graph_data:
            return format_graph_response(graph, {"source": source, "target": target}, {},
                                        ["Graph not found or empty"])

        edges = graph_data.get("edges", [])
        adj = defaultdict(list)

        for edge in edges:
            src, tgt = edge.get("source"), edge.get("target")
            adj[src].append((tgt, edge.get("relationship")))
            if not directed:
                adj[tgt].append((src, edge.get("relationship")))

        # BFS to find path
        queue = [(source, [source], [])]
        paths = []

        while queue and len(paths) < 10:
            node, path, rels = queue.pop(0)

            if len(path) - 1 > max_depth:
                continue

            if node == target:
                paths.append({"path": path, "relationships": rels})
                continue

            for neighbor, rel in adj.get(node, []):
                if neighbor not in path:
                    queue.append((neighbor, path + [neighbor], rels + [rel]))

        return format_graph_response(graph, {"source": source, "target": target},
                                    {"paths": paths})
    except Exception as e:
        log(f"ERROR in graph_find_path: {e}")
        return format_graph_response(graph, {"source": source, "target": target},
                                    {}, [str(e)])


def graph_get_subgraph(seed_nodes: List[str], graph: str = "code",
                      depth: int = 1) -> dict:
    """Get bounded subgraph around seed nodes."""
    try:
        if graph == "database":
            success, error = refresh_database_graph()
            if not success:
                return format_graph_response("database", {"seed_nodes": seed_nodes},
                                            {}, [f"Graph refresh failed: {error}"])
            graph_data = graph_io.load_database_graph()
        else:
            graph_data = graph_io.load_code_graph()

        if not graph_data:
            return format_graph_response(graph, {"seed_nodes": seed_nodes}, {},
                                        ["Graph not found or empty"])

        all_nodes = {n.get("id"): n for n in graph_data.get("nodes", [])}
        edges = graph_data.get("edges", [])

        # Collect nodes within depth from seeds
        visited = set(seed_nodes)
        current_level = set(seed_nodes)

        for _ in range(depth):
            next_level = set()
            for node_id in current_level:
                for edge in edges:
                    if edge.get("source") == node_id:
                        next_level.add(edge.get("target"))
                    elif edge.get("target") == node_id:
                        next_level.add(edge.get("source"))
            visited.update(next_level)
            current_level = next_level

        # Collect subgraph
        sub_nodes = [all_nodes[nid] for nid in visited if nid in all_nodes]
        sub_edges = [e for e in edges
                    if e.get("source") in visited and e.get("target") in visited]

        return format_graph_response(graph, {"seed_nodes": seed_nodes, "depth": depth},
                                    {"nodes": sub_nodes, "edges": sub_edges})
    except Exception as e:
        log(f"ERROR in graph_get_subgraph: {e}")
        return format_graph_response(graph, {"seed_nodes": seed_nodes}, {}, [str(e)])


def graph_status(graph: str = "code") -> dict:
    """Return graph status, generation timestamp, and staleness info."""
    import json
    from datetime import datetime

    try:
        repo_root = graph_io.get_repo_root()

        if graph == "database":
            graph_path = repo_root / ".agent-os" / "db" / "db_graph.json"
        else:
            graph_path = repo_root / "graphify-out" / "graph.json"

        status = {
            "exists": graph_path.exists(),
            "path": str(graph_path)
        }

        if graph_path.exists():
            stat = graph_path.stat()
            status["generated_at"] = datetime.fromtimestamp(stat.st_mtime).isoformat()
            status["file_size"] = stat.st_size

            with open(graph_path, encoding="utf-8") as f:
                data = json.load(f)
                status["node_count"] = len(data.get("nodes", []))
                # The graphify (code) graph stores edges under "links"; the DB
                # graph uses "edges". Count whichever is present.
                status["edge_count"] = len(data.get("edges") or data.get("links") or [])

        return format_graph_response(graph, {}, status)
    except Exception as e:
        log(f"ERROR in graph_status: {e}")
        return format_graph_response(graph, {}, {}, [str(e)])


def graph_refresh(graph: str = "code") -> dict:
    """Explicitly rebuild the selected graph."""
    try:
        if graph == "database":
            success, error = refresh_database_graph()
            if success:
                return format_graph_response("database", {}, {"status": "refreshed"})
            else:
                return format_graph_response("database", {}, {},
                                            [error or "database graph refresh failed"])
        else:
            # For graphify: run update command. Resolve the console script
            # venv-safely (WINDOWS.md §2) rather than trusting a bare name on
            # PATH, which the plugin's own venv does not expose.
            try:
                repo_root = graph_io.get_repo_root()
                # Force UTF-8 in the child and decode its pipes with
                # errors="replace": a cp1252 Windows console would otherwise make
                # a SUCCESSFUL graphify run raise UnicodeDecodeError in the parent
                # here and 500 the endpoint (WINDOWS.md §2/§3). Degraded-but-whole
                # output beats a false failure.
                child_env = dict(os.environ)
                child_env.setdefault("PYTHONIOENCODING", "utf-8")
                subprocess.run(
                    [graph_io.resolve_graphify(), "update", ".", "--force"],
                    cwd=repo_root,
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=child_env,
                    timeout=120,
                )
                return format_graph_response("code", {}, {"status": "refreshed"})
            except subprocess.CalledProcessError as e:
                return format_graph_response("code", {}, {}, [e.stderr])
    except Exception as e:
        log(f"ERROR in graph_refresh: {e}")
        return format_graph_response(graph, {}, {}, [str(e)])
