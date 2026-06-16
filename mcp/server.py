#!/usr/bin/env python3
"""
Jira-style task management MCP server using FastMCP and SQLite.
Repository-local task cards with status tracking and work logs.
"""

import os
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any
import sqlite3
import uuid
import atexit
import subprocess
from collections import defaultdict

from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
server = FastMCP("task-cards")

# Global database connection (initialized in setup)
db_conn: Optional[sqlite3.Connection] = None
db_path: Optional[Path] = None

# Flask app process (initialized in setup)
flask_process: Optional[subprocess.Popen] = None


def log(message: str):
    """Log a message to stdout with timestamp."""
    timestamp = datetime.now().isoformat()
    print(f"[{timestamp}] {message}", file=sys.stdout, flush=True)


def ensure_agent_os():
    """Ensure .agent-os directory and database exist."""
    global db_conn, db_path

    try:
        log("Initializing database connection...")

        # Find repo root by looking for .git first, then use current directory
        current_dir = Path.cwd()
        repo_root = current_dir

        # Look for .git to identify repo root
        search_dir = current_dir
        while search_dir != search_dir.parent:
            if (search_dir / ".git").exists():
                repo_root = search_dir
                log(f"Found repo root at: {repo_root}")
                break
            search_dir = search_dir.parent

        agent_os_dir = repo_root / ".agent-os"
        agent_os_dir.mkdir(exist_ok=True)
        log(f"Agent OS directory: {agent_os_dir}")

        db_path = agent_os_dir / "cards.sqlite"
        # check_same_thread=False is safe here because MCP runs in a single-threaded event loop.
        # The FastMCP framework serializes all tool calls through a single event loop,
        # so concurrent access from multiple threads is not possible.
        db_conn = sqlite3.connect(str(db_path), check_same_thread=False)
        db_conn.row_factory = sqlite3.Row
        log(f"Database connected: {db_path}")

        # Create tables if they don't exist
        cursor = db_conn.cursor()

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS cards (
            card_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT,
            status TEXT NOT NULL,
            priority TEXT,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS card_comments (
            comment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id TEXT NOT NULL,
            author TEXT,
            comment TEXT,
            created_at TIMESTAMP
        )
        """)

        db_conn.commit()
        log("Database tables initialized successfully")

        # Start the graph server after database initialization
        start_graph_server()

    except Exception as e:
        log(f"ERROR during database initialization: {e}")
        raise


def start_graph_server():
    """Start the Flask graph server in a subprocess."""
    global flask_process
    try:
        repo_root = get_repo_root()

        # Check multiple possible locations for app.py
        possible_paths = [
            repo_root / "mcp" / "db_tools" / "app.py",  # Installed in mcp/db_tools
            repo_root / "db_tools" / "app.py",          # Installed at repo root
        ]

        app_path = None
        for path in possible_paths:
            if path.exists():
                app_path = path
                break

        if not app_path:
            log(f"WARNING: Graph server app not found. Searched: {[str(p) for p in possible_paths]}")
            return

        # Start Flask server with stdout/stderr captured for error diagnostics
        flask_process = subprocess.Popen(
            [sys.executable, str(app_path)],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        log(f"Graph server started (PID: {flask_process.pid}) at {app_path}")
        log("📊 Graphs available at:")
        log("   - Database: http://localhost:5000/db_graph")
        log("   - Repository: http://localhost:5000/repo_graph")
        log("   - Home: http://localhost:5000/")

    except Exception as e:
        log(f"ERROR starting graph server: {e}")


def _check_graph_server_health():
    """Check if graph server is still running; log if it crashed."""
    global flask_process
    if flask_process and flask_process.poll() is not None:
        # Process has exited
        try:
            stdout, stderr = flask_process.communicate(timeout=1)
            if stderr:
                log(f"WARNING: Graph server crashed. stderr: {stderr[:200]}")
            if stdout:
                log(f"Graph server output: {stdout[:200]}")
        except Exception:
            pass
        flask_process = None
        log("WARNING: Graph server process exited unexpectedly")


def shutdown_db():
    """Properly close the database connection on shutdown."""
    global db_conn, flask_process

    # Shutdown Flask process
    if flask_process:
        try:
            flask_process.terminate()
            flask_process.wait(timeout=5)
            log("Graph server process terminated")
        except subprocess.TimeoutExpired:
            flask_process.kill()
            log("Graph server process killed")
        except Exception as e:
            log(f"ERROR shutting down graph server: {e}")

    # Close database connection
    if db_conn:
        try:
            db_conn.close()
            log("Database connection closed")
        except Exception as e:
            log(f"ERROR closing database: {e}")


# Register shutdown handler
atexit.register(shutdown_db)


# ============================================================================
# GRAPH TOOLS - Database and Graphify Integration
# ============================================================================

def get_repo_root() -> Path:
    """Find repository root by looking for .git directory."""
    current_dir = Path.cwd()
    search_dir = current_dir
    while search_dir != search_dir.parent:
        if (search_dir / ".git").exists():
            return search_dir
        search_dir = search_dir.parent
    return current_dir


def refresh_database_graph() -> tuple[bool, Optional[str]]:
    """Rebuild database graph by running build scripts. Returns (success, error_msg)."""
    try:
        repo_root = get_repo_root()

        # Run build_db_graph.py
        result = subprocess.run(
            [sys.executable, str(repo_root / "build_db_graph.py")],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30
        )
        log(f"Database graph rebuilt: {result.stdout[:100]}")

        # Run build_graph_html.py
        result = subprocess.run(
            [sys.executable, str(repo_root / "build_graph_html.py")],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30
        )
        log(f"Graph HTML built: {result.stdout[:100]}")

        return True, None
    except subprocess.CalledProcessError as e:
        error_msg = f"Script failed with exit code {e.returncode}: {e.stderr}"
        log(f"ERROR refreshing database graph: {error_msg}")
        return False, error_msg
    except Exception as e:
        error_msg = str(e)
        log(f"ERROR refreshing database graph: {error_msg}")
        return False, error_msg


def load_database_graph() -> Optional[Dict[str, Any]]:
    """Load the database graph from .agent-os/db/db_graph.json."""
    try:
        _check_graph_server_health()
        repo_root = get_repo_root()
        graph_path = repo_root / ".agent-os" / "db" / "db_graph.json"

        if not graph_path.exists():
            return None

        with open(graph_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        log(f"ERROR loading database graph: {e}")
        return None


def load_code_graph() -> Optional[Dict[str, Any]]:
    """Load the code graph from graphify-out/graph.json."""
    try:
        _check_graph_server_health()
        repo_root = get_repo_root()
        graph_path = repo_root / "graphify-out" / "graph.json"

        if not graph_path.exists():
            return None

        with open(graph_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        log(f"ERROR loading code graph: {e}")
        return None


def format_graph_response(graph_type: str, query: Dict, results: Dict,
                         warnings: List[str] = None, truncated: bool = False) -> Dict:
    """Format response in standard graph tool response format."""
    return {
        "graph": graph_type,
        "generated_at": datetime.utcnow().isoformat(),
        "query": query,
        "results": results,
        "warnings": warnings or [],
        "truncated": truncated
    }


# ============================================================================
# Shared Graph Tools (1-7)
# ============================================================================

@server.tool()
def graph_search_nodes(query: str, graph: str = "code", node_type: Optional[str] = None,
                       fuzzy: bool = False, limit: int = 50) -> dict:
    """Search nodes by name, qualified name, type, and metadata."""
    try:
        if graph == "database":
            success, error = refresh_database_graph()
            if not success:
                return format_graph_response("database", {"query": query}, {},
                                            [f"Graph refresh failed: {error}"])
            graph_data = load_database_graph()
        else:
            graph_data = load_code_graph()

        if not graph_data:
            return format_graph_response(graph, {"query": query}, {},
                                        ["Graph not found or empty"])

        results = []
        nodes = graph_data.get("nodes", [])

        for node in nodes:
            node_id = node.get("id", "")
            label = node.get("label", "")
            node_types = node.get("type", "")

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

        truncated = len(nodes) > limit
        return format_graph_response(graph, {"query": query, "type": node_type},
                                    {"nodes": results}, truncated=truncated)
    except Exception as e:
        log(f"ERROR in graph_search_nodes: {e}")
        return format_graph_response(graph, {"query": query}, {}, [str(e)])


@server.tool()
def graph_get_node(node_id: str, graph: str = "code") -> dict:
    """Get complete metadata for one node."""
    try:
        if graph == "database":
            success, error = refresh_database_graph()
            if not success:
                return format_graph_response("database", {"node_id": node_id}, {},
                                            [f"Graph refresh failed: {error}"])
            graph_data = load_database_graph()
        else:
            graph_data = load_code_graph()

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


@server.tool()
def graph_get_neighbors(node_id: str, graph: str = "code", direction: str = "both",
                        depth: int = 1, relationship: Optional[str] = None) -> dict:
    """Get incoming, outgoing, or bidirectional neighbors."""
    try:
        if graph == "database":
            success, error = refresh_database_graph()
            if not success:
                return format_graph_response("database", {"node_id": node_id}, {},
                                            [f"Graph refresh failed: {error}"])
            graph_data = load_database_graph()
        else:
            graph_data = load_code_graph()

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


@server.tool()
def graph_find_path(source: str, target: str, graph: str = "code",
                    max_depth: int = 5, directed: bool = True) -> dict:
    """Find paths between nodes with maximum depth."""
    try:
        if graph == "database":
            success, error = refresh_database_graph()
            if not success:
                return format_graph_response("database", {"source": source, "target": target},
                                            {}, [f"Graph refresh failed: {error}"])
            graph_data = load_database_graph()
        else:
            graph_data = load_code_graph()

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


@server.tool()
def graph_get_subgraph(seed_nodes: List[str], graph: str = "code",
                      depth: int = 1) -> dict:
    """Get bounded subgraph around seed nodes."""
    try:
        if graph == "database":
            success, error = refresh_database_graph()
            if not success:
                return format_graph_response("database", {"seed_nodes": seed_nodes},
                                            {}, [f"Graph refresh failed: {error}"])
            graph_data = load_database_graph()
        else:
            graph_data = load_code_graph()

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


@server.tool()
def graph_status(graph: str = "code") -> dict:
    """Return graph status, generation timestamp, and staleness info."""
    try:
        repo_root = get_repo_root()

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

            with open(graph_path) as f:
                data = json.load(f)
                status["node_count"] = len(data.get("nodes", []))
                status["edge_count"] = len(data.get("edges", []))

        return format_graph_response(graph, {}, status)
    except Exception as e:
        log(f"ERROR in graph_status: {e}")
        return format_graph_response(graph, {}, {}, [str(e)])


@server.tool()
def graph_refresh(graph: str = "code") -> dict:
    """Explicitly rebuild the selected graph."""
    try:
        if graph == "database":
            success, error = refresh_database_graph()
            if success:
                return format_graph_response("database", {}, {"status": "refreshed"})
            else:
                return format_graph_response("database", {}, {}, [error])
        else:
            # For graphify: run update command
            try:
                repo_root = get_repo_root()
                result = subprocess.run(
                    ["graphify", "--update"],
                    cwd=repo_root,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                return format_graph_response("code", {}, {"status": "refreshed"})
            except subprocess.CalledProcessError as e:
                return format_graph_response("code", {}, {}, [e.stderr])
    except Exception as e:
        log(f"ERROR in graph_refresh: {e}")
        return format_graph_response(graph, {}, {}, [str(e)])


# ============================================================================
# Database Graph Tools (8-13) - All require refresh_database_graph()
# ============================================================================

@server.tool()
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


@server.tool()
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


@server.tool()
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


@server.tool()
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


@server.tool()
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


@server.tool()
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


# ============================================================================
# Code Graph Tools (14-18) - Use graphify code graph
# ============================================================================

@server.tool()
def code_get_symbol(symbol_name: str) -> dict:
    """Get code symbol with source location, callers, callees, and imports."""
    try:
        graph_data = load_code_graph()
        if not graph_data:
            return format_graph_response("code", {"symbol": symbol_name}, {},
                                        ["Code graph not found"])

        nodes = graph_data.get("nodes", [])
        symbol_node = None

        for node in nodes:
            if symbol_name in node.get("id", "") or symbol_name == node.get("label", ""):
                symbol_node = node
                break

        if not symbol_node:
            return format_graph_response("code", {"symbol": symbol_name}, {},
                                        [f"Symbol {symbol_name} not found"])

        edges = graph_data.get("edges", [])
        callers = []
        callees = []
        imports = []

        node_id = symbol_node.get("id")
        for edge in edges:
            if edge.get("target") == node_id and "calls" in edge.get("relationship", ""):
                callers.append({
                    "source": edge.get("source"),
                    "relationship": edge.get("relationship")
                })
            elif edge.get("source") == node_id and "calls" in edge.get("relationship", ""):
                callees.append({
                    "target": edge.get("target"),
                    "relationship": edge.get("relationship")
                })
            elif edge.get("relationship") == "imports":
                if edge.get("source") == node_id:
                    imports.append(edge.get("target"))
                elif edge.get("target") == node_id:
                    imports.append(edge.get("source"))

        return format_graph_response("code", {"symbol": symbol_name}, {
            "symbol": symbol_node,
            "callers": callers,
            "callees": callees,
            "imports": imports
        })
    except Exception as e:
        log(f"ERROR in code_get_symbol: {e}")
        return format_graph_response("code", {"symbol": symbol_name}, {}, [str(e)])


@server.tool()
def code_search_symbols(query: str, symbol_type: Optional[str] = None) -> dict:
    """Search classes, functions, methods, modules, files, interfaces."""
    try:
        graph_data = load_code_graph()
        if not graph_data:
            return format_graph_response("code", {"query": query}, {},
                                        ["Code graph not found"])

        nodes = graph_data.get("nodes", [])
        results = []

        for node in nodes:
            node_type = node.get("type", "")
            if symbol_type and symbol_type != node_type:
                continue

            if query.lower() in node.get("id", "").lower() or \
               query.lower() in node.get("label", "").lower():
                results.append({
                    "id": node.get("id"),
                    "label": node.get("label"),
                    "type": node_type,
                    "source_location": node.get("source_location")
                })

        return format_graph_response("code", {"query": query, "type": symbol_type},
                                    {"symbols": results})
    except Exception as e:
        log(f"ERROR in code_search_symbols: {e}")
        return format_graph_response("code", {"query": query}, {}, [str(e)])


@server.tool()
def code_get_dependencies(symbol_name: str, depth: int = 2) -> dict:
    """Get incoming and outgoing dependencies with depth control."""
    try:
        graph_data = load_code_graph()
        if not graph_data:
            return format_graph_response("code", {"symbol": symbol_name, "depth": depth},
                                        {}, ["Code graph not found"])

        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])

        symbol_node = None
        for node in nodes:
            if symbol_name in node.get("id", ""):
                symbol_node = node
                break

        if not symbol_node:
            return format_graph_response("code", {"symbol": symbol_name}, {},
                                        [f"Symbol {symbol_name} not found"])

        # BFS to find dependencies
        adj = defaultdict(list)
        for edge in edges:
            src, tgt = edge.get("source"), edge.get("target")
            rel = edge.get("relationship", "")
            adj[src].append((tgt, rel))
            adj[tgt].append((src, rel))

        visited = set()
        queue = [(symbol_node.get("id"), 0, "start")]
        dependencies = {"incoming": [], "outgoing": []}

        while queue:
            node_id, d, rel = queue.pop(0)
            if d > depth or node_id in visited:
                continue
            visited.add(node_id)

            for neighbor, rel_type in adj.get(node_id, []):
                if neighbor not in visited and d < depth:
                    queue.append((neighbor, d + 1, rel_type))

                    if rel_type in ["depends-on", "imports", "uses"]:
                        dependencies["incoming"].append({
                            "source": neighbor,
                            "relationship": rel_type,
                            "depth": d + 1
                        })
                    else:
                        dependencies["outgoing"].append({
                            "target": neighbor,
                            "relationship": rel_type,
                            "depth": d + 1
                        })

        return format_graph_response("code", {"symbol": symbol_name, "depth": depth},
                                    dependencies)
    except Exception as e:
        log(f"ERROR in code_get_dependencies: {e}")
        return format_graph_response("code", {"symbol": symbol_name}, {}, [str(e)])


@server.tool()
def code_find_callers(symbol_name: str, transitive: bool = False,
                      max_depth: int = 5) -> dict:
    """Find direct and transitive callers of a symbol."""
    try:
        graph_data = load_code_graph()
        if not graph_data:
            return format_graph_response("code", {"symbol": symbol_name}, {},
                                        ["Code graph not found"])

        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])

        symbol_node = None
        for node in nodes:
            if symbol_name in node.get("id", ""):
                symbol_node = node
                break

        if not symbol_node:
            return format_graph_response("code", {"symbol": symbol_name}, {},
                                        [f"Symbol {symbol_name} not found"])

        # Find all call edges pointing to this symbol
        callers = []
        node_id = symbol_node.get("id")

        for edge in edges:
            if edge.get("target") == node_id and "call" in edge.get("relationship", ""):
                callers.append({
                    "caller": edge.get("source"),
                    "relationship": edge.get("relationship"),
                    "depth": 1
                })

        # If transitive, find callers of callers
        if transitive:
            visited = {node_id}
            queue = [(c["caller"], 2) for c in callers]

            while queue and len(callers) < 100:
                curr, d = queue.pop(0)
                if d > max_depth or curr in visited:
                    continue
                visited.add(curr)

                for edge in edges:
                    if edge.get("target") == curr and "call" in edge.get("relationship", ""):
                        caller = edge.get("source")
                        callers.append({
                            "caller": caller,
                            "relationship": edge.get("relationship"),
                            "depth": d
                        })
                        queue.append((caller, d + 1))

        return format_graph_response("code", {"symbol": symbol_name, "transitive": transitive},
                                    {"callers": callers})
    except Exception as e:
        log(f"ERROR in code_find_callers: {e}")
        return format_graph_response("code", {"symbol": symbol_name}, {}, [str(e)])


@server.tool()
def code_impact_analysis(symbol_name: str) -> dict:
    """Analyze likely affected code for a symbol change."""
    try:
        graph_data = load_code_graph()
        if not graph_data:
            return format_graph_response("code", {"symbol": symbol_name}, {},
                                        ["Code graph not found"])

        nodes = graph_data.get("nodes", [])
        edges = graph_data.get("edges", [])

        symbol_node = None
        for node in nodes:
            if symbol_name in node.get("id", ""):
                symbol_node = node
                break

        if not symbol_node:
            return format_graph_response("code", {"symbol": symbol_name}, {},
                                        [f"Symbol {symbol_name} not found"])

        node_id = symbol_node.get("id")
        impact = {
            "direct_callers": [],
            "modules_affected": [],
            "tests": [],
            "interfaces": [],
            "entry_points": []
        }

        for edge in edges:
            if edge.get("target") == node_id and "call" in edge.get("relationship", ""):
                impact["direct_callers"].append(edge.get("source"))
            elif edge.get("source") == node_id:
                rel = edge.get("relationship", "")
                target = edge.get("target")

                if "test" in target.lower():
                    impact["tests"].append(target)
                elif "interface" in rel or "interface" in target.lower():
                    impact["interfaces"].append(target)
                elif "entry" in rel or "entry" in target.lower():
                    impact["entry_points"].append(target)
                else:
                    impact["modules_affected"].append(target)

        return format_graph_response("code", {"symbol": symbol_name}, impact)
    except Exception as e:
        log(f"ERROR in code_impact_analysis: {e}")
        return format_graph_response("code", {"symbol": symbol_name}, {}, [str(e)])


@server.tool()
def create_card(title: str, description: Optional[str] = None, priority: str = "medium") -> dict:
    """Create a new task card."""
    try:
        if not db_conn:
            raise RuntimeError("Database not initialized. Server may not have started properly.")

        card_id = str(uuid.uuid4())[:8]
        now = datetime.utcnow().isoformat()

        cursor = db_conn.cursor()
        cursor.execute("""
        INSERT INTO cards (card_id, title, description, status, priority, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (card_id, title, description, "Created", priority, now, now))

        db_conn.commit()
        log(f"Created card {card_id}: {title}")

        return {
            "card_id": card_id,
            "title": title,
            "description": description,
            "status": "Created",
            "priority": priority,
            "created_at": now
        }
    except Exception as e:
        log(f"ERROR creating card: {e}")
        return {"error": f"Failed to create card: {str(e)}"}


@server.tool()
def list_cards(status: Optional[str] = None, priority: Optional[str] = None) -> dict:
    """List cards with optional filters."""
    try:
        if not db_conn:
            raise RuntimeError("Database not initialized. Server may not have started properly.")

        cursor = db_conn.cursor()

        query = "SELECT * FROM cards WHERE 1=1"
        params = []

        if status:
            query += " AND status = ?"
            params.append(status)

        if priority:
            query += " AND priority = ?"
            params.append(priority)

        query += " ORDER BY created_at DESC"
        cursor.execute(query, params)

        cards = []
        for row in cursor.fetchall():
            cards.append(dict(row))

        log(f"Listed {len(cards)} cards (status={status}, priority={priority})")
        return {
            "cards": cards,
            "total": len(cards)
        }
    except Exception as e:
        log(f"ERROR listing cards: {e}")
        return {"error": f"Failed to list cards: {str(e)}", "cards": [], "total": 0}


@server.tool()
def get_card(card_id: str) -> dict:
    """Retrieve a card by card_id."""
    try:
        if not db_conn:
            raise RuntimeError("Database not initialized. Server may not have started properly.")

        cursor = db_conn.cursor()

        # Get card
        cursor.execute("SELECT * FROM cards WHERE card_id = ?", (card_id,))
        card_row = cursor.fetchone()

        if not card_row:
            log(f"Card not found: {card_id}")
            return {"error": f"Card {card_id} not found"}

        card = dict(card_row)

        # Get comments
        cursor.execute("""
        SELECT comment_id, author, comment, created_at FROM card_comments
        WHERE card_id = ? ORDER BY created_at ASC
        """, (card_id,))

        comments = [dict(row) for row in cursor.fetchall()]
        card["comments"] = comments

        log(f"Retrieved card {card_id} with {len(comments)} comments")
        return card
    except Exception as e:
        log(f"ERROR retrieving card {card_id}: {e}")
        return {"error": f"Failed to retrieve card: {str(e)}"}


@server.tool()
def update_card(card_id: str, title: Optional[str] = None,
                description: Optional[str] = None, priority: Optional[str] = None,
                status: Optional[str] = None) -> dict:
    """Update a card's fields."""
    try:
        if not db_conn:
            raise RuntimeError("Database not initialized. Server may not have started properly.")

        cursor = db_conn.cursor()

        # Build dynamic update query
        updates = []
        params = []

        if title is not None:
            updates.append("title = ?")
            params.append(title)

        if description is not None:
            updates.append("description = ?")
            params.append(description)

        if priority is not None:
            updates.append("priority = ?")
            params.append(priority)

        if status is not None:
            updates.append("status = ?")
            params.append(status)

        if not updates:
            return {"error": "No fields to update"}

        updates.append("updated_at = ?")
        params.append(datetime.utcnow().isoformat())
        params.append(card_id)

        query = f"UPDATE cards SET {', '.join(updates)} WHERE card_id = ?"
        cursor.execute(query, params)
        db_conn.commit()

        log(f"Updated card {card_id}")

        # Return updated card
        return get_card(card_id)
    except Exception as e:
        log(f"ERROR updating card {card_id}: {e}")
        return {"error": f"Failed to update card: {str(e)}"}


@server.tool()
def add_comment(card_id: str, author: str, comment: str) -> dict:
    """Add a work log entry/comment to a card."""
    try:
        if not db_conn:
            raise RuntimeError("Database not initialized. Server may not have started properly.")

        cursor = db_conn.cursor()

        # Verify card exists
        cursor.execute("SELECT card_id FROM cards WHERE card_id = ?", (card_id,))
        if not cursor.fetchone():
            log(f"Card not found for comment: {card_id}")
            return {"error": f"Card {card_id} not found"}

        now = datetime.utcnow().isoformat()

        cursor.execute("""
        INSERT INTO card_comments (card_id, author, comment, created_at)
        VALUES (?, ?, ?, ?)
        """, (card_id, author, comment, now))

        db_conn.commit()

        cursor.execute("SELECT comment_id FROM card_comments WHERE card_id = ? ORDER BY comment_id DESC LIMIT 1", (card_id,))
        result = cursor.fetchone()

        if not result:
            raise RuntimeError("Failed to retrieve inserted comment")

        comment_id = result[0]

        log(f"Added comment {comment_id} to card {card_id}")

        return {
            "comment_id": comment_id,
            "card_id": card_id,
            "author": author,
            "comment": comment,
            "created_at": now
        }
    except Exception as e:
        log(f"ERROR adding comment to card {card_id}: {e}")
        return {"error": f"Failed to add comment: {str(e)}"}


@server.tool()
def complete_card(card_id: str, completion_summary: str) -> dict:
    """Mark a card as Complete with a summary."""
    try:
        if not db_conn:
            raise RuntimeError("Database not initialized. Server may not have started properly.")

        log(f"Completing card {card_id}")

        # Add completion summary as final comment
        add_comment(card_id, "system", f"Completion: {completion_summary}")

        # Update status
        result = update_card(card_id, status="Complete")
        log(f"Card {card_id} marked as Complete")
        return result
    except Exception as e:
        log(f"ERROR completing card {card_id}: {e}")
        return {"error": f"Failed to complete card: {str(e)}"}


if __name__ == "__main__":
    try:
        log("Starting Task Cards MCP Server...")
        ensure_agent_os()
        log("Database initialized, starting server...")
        server.run()
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        sys.exit(1)
    finally:
        log("Server shutdown")
