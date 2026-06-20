#!/usr/bin/env python3
"""
Jira-style task management MCP server using FastMCP and SQLite.
Repository-local task cards with status tracking and work logs.

This file is the thin FastMCP entrypoint.  All tool implementations live in
focused sub-modules:

    card_tools.py          - create_card, list_cards, get_card, update_card,
                             add_comment, complete_card
    shared_graph_tools.py  - graph_search_nodes, graph_get_node,
                             graph_get_neighbors, graph_find_path,
                             graph_get_subgraph, graph_status, graph_refresh
    db_graph_tools.py      - db_get_table, db_get_column, db_search_schema,
                             db_get_table_relationships,
                             db_find_relationship_path,
                             db_get_routine_dependencies
    code_graph_tools.py    - code_get_symbol, code_search_symbols,
                             code_get_dependencies, code_find_callers,
                             code_impact_analysis
    graph_io.py            - get_repo_root, find_db_tools_dir,
                             refresh_database_graph, load_database_graph,
                             load_code_graph, format_graph_response, log
    graph_server.py        - start_graph_server, _graph_port, _port_in_use,
                             _check_graph_server_health, shutdown_flask
"""

import atexit
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

# Sub-modules (all non-relative so server.py runs as a plain script).
import card_tools
import code_graph_tools
import db_graph_tools
import graph_io
import graph_server
import shared_graph_tools

# Re-export helpers so existing callers (e.g. test_server.py) can still do
# ``server.log(...)`` or ``server.get_repo_root()``.
from graph_io import log, get_repo_root, format_graph_response
from graph_server import start_graph_server, _check_graph_server_health

# Initialize FastMCP server
server = FastMCP("task-cards")

# Global database connection (initialized in setup)
db_conn: Optional[sqlite3.Connection] = None
db_path: Optional[Path] = None

# Allowed card lifecycle states (kept for any external consumer that imports it)
VALID_STATUSES = ("Created", "In Progress", "Complete")


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

        # Inject the live connection into card_tools so its functions can use it.
        card_tools.set_db_conn(db_conn)

        # Start the graph server after database initialization
        start_graph_server()

    except Exception as e:
        log(f"ERROR during database initialization: {e}")
        raise


def shutdown_db():
    """Properly close the database connection on shutdown."""
    global db_conn

    # Shutdown Flask process
    graph_server.shutdown_flask()

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
# Register all MCP tools
# ============================================================================

# Card tools (6)
create_card = server.tool()(card_tools.create_card)
list_cards = server.tool()(card_tools.list_cards)
get_card = server.tool()(card_tools.get_card)
update_card = server.tool()(card_tools.update_card)
add_comment = server.tool()(card_tools.add_comment)
complete_card = server.tool()(card_tools.complete_card)

# Shared graph tools (7)
graph_search_nodes = server.tool()(shared_graph_tools.graph_search_nodes)
graph_get_node = server.tool()(shared_graph_tools.graph_get_node)
graph_get_neighbors = server.tool()(shared_graph_tools.graph_get_neighbors)
graph_find_path = server.tool()(shared_graph_tools.graph_find_path)
graph_get_subgraph = server.tool()(shared_graph_tools.graph_get_subgraph)
graph_status = server.tool()(shared_graph_tools.graph_status)
graph_refresh = server.tool()(shared_graph_tools.graph_refresh)

# Database graph tools (6)
db_get_table = server.tool()(db_graph_tools.db_get_table)
db_get_column = server.tool()(db_graph_tools.db_get_column)
db_search_schema = server.tool()(db_graph_tools.db_search_schema)
db_get_table_relationships = server.tool()(db_graph_tools.db_get_table_relationships)
db_find_relationship_path = server.tool()(db_graph_tools.db_find_relationship_path)
db_get_routine_dependencies = server.tool()(db_graph_tools.db_get_routine_dependencies)

# Code graph tools (5)
code_get_symbol = server.tool()(code_graph_tools.code_get_symbol)
code_search_symbols = server.tool()(code_graph_tools.code_search_symbols)
code_get_dependencies = server.tool()(code_graph_tools.code_get_dependencies)
code_find_callers = server.tool()(code_graph_tools.code_find_callers)
code_impact_analysis = server.tool()(code_graph_tools.code_impact_analysis)


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
