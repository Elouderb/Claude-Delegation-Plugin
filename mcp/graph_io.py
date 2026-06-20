"""
Graph loading and formatting helpers.

Provides get_repo_root(), find_db_tools_dir(), refresh_database_graph(),
load_database_graph(), load_code_graph(), and format_graph_response().
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def log(message: str):
    """Log a message to stderr with timestamp.

    Must be stderr, not stdout: the MCP stdio transport speaks JSON-RPC over
    stdout, so any stdout writes corrupt the protocol stream and break the
    client handshake. Claude Code captures stderr as plugin logs.
    """
    timestamp = datetime.now().isoformat()
    print(f"[{timestamp}] {message}", file=sys.stderr, flush=True)


def get_repo_root() -> Path:
    """Find repository root by looking for .git directory."""
    current_dir = Path.cwd()
    search_dir = current_dir
    while search_dir != search_dir.parent:
        if (search_dir / ".git").exists():
            return search_dir
        search_dir = search_dir.parent
    return current_dir


def find_db_tools_dir() -> Optional[Path]:
    """Locate the db_tools directory containing the graph build scripts.

    Checks alongside this server file first, then common install layouts.
    """
    candidates = [
        Path(__file__).resolve().parent / "db_tools",  # mcp/db_tools (next to server.py)
        get_repo_root() / "mcp" / "db_tools",          # Installed under mcp/
        get_repo_root() / "db_tools",                  # Installed at repo root
    ]
    for path in candidates:
        if (path / "build_db_graph.py").exists():
            return path
    return None


def refresh_database_graph() -> tuple[bool, Optional[str]]:
    """Rebuild database graph by running build scripts. Returns (success, error_msg)."""
    try:
        repo_root = get_repo_root()
        db_tools = find_db_tools_dir()
        if db_tools is None:
            error_msg = "Graph build scripts not found (build_db_graph.py / build_graph_html.py)"
            log(f"ERROR refreshing database graph: {error_msg}")
            return False, error_msg

        # Run build_db_graph.py (writes to .agent-os/db relative to cwd=repo_root)
        result = subprocess.run(
            [sys.executable, str(db_tools / "build_db_graph.py")],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=30
        )
        log(f"Database graph rebuilt: {result.stdout[:100]}")

        # Run build_graph_html.py
        result = subprocess.run(
            [sys.executable, str(db_tools / "build_graph_html.py")],
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
        from graph_server import _check_graph_server_health
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
        from graph_server import _check_graph_server_health
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
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "results": results,
        "warnings": warnings or [],
        "truncated": truncated
    }
