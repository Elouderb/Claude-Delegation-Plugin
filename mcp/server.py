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
    memory_tools.py        - memory_ingest, memory_query, memory_status
                             (OPTIONAL central vector-memory subsystem; degrades
                             gracefully when its heavy deps are absent)
    graph_io.py            - get_repo_root, find_db_tools_dir,
                             refresh_database_graph, load_database_graph,
                             load_code_graph, format_graph_response, log
    graph_server.py        - start_graph_server, _graph_port, _port_in_use,
                             _check_graph_server_health, shutdown_flask
"""

import atexit
import sys
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

# Put the REPO ROOT on sys.path so the top-level ``contracts`` and ``outbox``
# packages (siblings of ``mcp/``) import by bare name from every mcp sub-module —
# the same convention the test-suite uses (sys.path.insert of parents[2]). This
# runs before the sub-modules below so their guarded ``import outbox`` resolves
# in the running server; a checkout without the packages simply degrades to
# no-op event emission (the imports there are guarded).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Sub-modules (all non-relative so server.py runs as a plain script).
import card_tools
import code_graph_tools
import db_graph_tools
import graph_io
import graph_server
import memory_tools
import shared_graph_tools
import sync_server

# Re-export helpers so existing callers (e.g. test_server.py) can still do
# ``server.log(...)`` or ``server.get_repo_root()``.
from graph_io import log, get_repo_root, format_graph_response
from graph_server import start_graph_server, _check_graph_server_health

# Initialize FastMCP server
server = FastMCP("task-cards")

# Resolved path to the repo-local card database.  Card operations open their own
# short-lived connection per call (see card_tools._connect), so the server holds
# no long-lived handle that could be stranded when cards.sqlite is replaced.
db_path: Optional[Path] = None

# Allowed card lifecycle states (kept for any external consumer that imports it)
VALID_STATUSES = ("Created", "In Progress", "Complete")


def _ensure_agent_os_gitignore(agent_os_dir: Path) -> None:
    """Create .agent-os/.gitignore with a wildcard if it does not already exist.

    The wildcard makes git ignore everything under .agent-os/ (cards.sqlite,
    db/, hooks/) regardless of the project's root .gitignore.  Write is
    idempotent: an existing file is never overwritten.  Any OS error is
    swallowed so protection is best-effort and never breaks card operations.
    """
    try:
        gitignore_path = agent_os_dir / ".gitignore"
        if not gitignore_path.exists():
            gitignore_path.write_text("*\n", encoding="utf-8")
    except OSError:
        pass


def ensure_agent_os():
    """Ensure the .agent-os directory and card database schema exist.

    Resolves the repo-local card database path and hands it to card_tools, which
    opens a fresh connection per operation.  The schema is created up front (and
    re-ensured on every connection) so a missing or replaced cards.sqlite file
    self-heals instead of stranding a long-lived read-only handle.
    """
    global db_path

    try:
        log("Initializing database...")

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
        _ensure_agent_os_gitignore(agent_os_dir)
        log(f"Agent OS directory: {agent_os_dir}")

        db_path = agent_os_dir / "cards.sqlite"

        # Point card_tools at the path (it opens a short-lived connection per
        # call) and create the file + schema once up front.
        card_tools.set_db_path(db_path)
        card_tools.init_db()
        log(f"Database ready (per-operation connections): {db_path}")

        # Record the SINGLE source of truth for the event-emission repo root:
        # the same repo_root identity is bootstrapped under, so card events and
        # graph events for this repo always land in the same outbox (finding 4).
        graph_io.set_emission_repo_root(repo_root)

        # Bootstrap machine + repository identity (Phase 1b). Cheap and idempotent
        # (identity helpers are atomic/self-healing); the git-remote probe runs
        # only on a checkout's first ever bootstrap. Guarded end-to-end so a
        # missing outbox/contracts package or a no-git checkout never blocks
        # server startup — event emission simply no-ops without identity.
        try:
            import outbox
            outbox.bootstrap_identity(repo_root)
            log("Identity bootstrapped (machine + repository)")
        except Exception as e:
            log(f"Identity bootstrap skipped: {e}")

        # --- Companion startup is FULLY ISOLATED from card-tool availability ---
        # Card tools are registered at import and only SERVED once server.run() is
        # reached (below, in __main__). ensure_agent_os() must therefore always
        # return so run() is reached: any companion failure — a held/stale/foreign
        # port, a Job-Object/spawn error, a wedged old graph server — must NOT
        # propagate here, or the outer `except: raise` would sys.exit(1) BEFORE
        # run() and take EVERY tool (card tools included) down. That regression is
        # exactly the M2 field report. Each companion start already self-guards;
        # these belts double-guard the invariant and keep it true as the spawn path
        # grows (bounded, never-hang). The graph-server startup itself is internally
        # time-bounded (stale-port recovery never blocks).
        try:
            start_graph_server()
        except Exception as e:
            log(f"WARNING: graph server startup failed; card tools remain available: {e}")

        # The outbox drain companion — OFF by default (AGENT_OS_SYNC=1 opts in).
        # Spawned like the graph server (pdeathsig/Job-Object/respawn/log-file); a
        # no-op unless enabled, so a stock install never phones home without consent.
        try:
            sync_server.start_sync_worker(repo_root)
        except Exception as e:
            log(f"WARNING: sync worker startup failed; card tools remain available: {e}")

    except Exception as e:
        log(f"ERROR during database initialization: {e}")
        raise


def shutdown_db():
    """Shut down the graph server.

    Card operations use short-lived per-call connections, so there is no
    long-lived card-database handle to close here.
    """
    graph_server.shutdown_flask()
    sync_server.shutdown_sync_worker()


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

# Memory tools (3) — OPTIONAL central vector-memory subsystem.  Registering them
# never imports the heavy deps; each tool reports unavailability at call time if
# the extras (mcp/memory_requirements.txt) are not installed.
memory_ingest = server.tool()(memory_tools.memory_ingest)
memory_query = server.tool()(memory_tools.memory_query)
memory_status = server.tool()(memory_tools.memory_status)


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
