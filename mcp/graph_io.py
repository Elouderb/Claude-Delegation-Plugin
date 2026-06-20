"""
Graph loading and formatting helpers.

Provides get_repo_root(), find_db_tools_dir(), refresh_database_graph(),
load_database_graph(), load_code_graph(), and format_graph_response().

In-process build
----------------
On a TTL cache miss, refresh_database_graph() builds the database graph
IN-PROCESS — it imports the build core from db_tools/build_db_graph.py and
reuses a module-level pooled pyodbc connection (opened lazily, reconnected on
failure) rather than spawning a subprocess.  The tool path builds DATA ONLY
(db_graph.json + .md + .graphml); the pyvis HTML is built only by the Flask UI
refresh endpoint.

TTL / staleness contract for refresh_database_graph()
------------------------------------------------------
When AGENT_OS_DB_GRAPH_TTL is set to a positive integer (default 30),
consecutive db_* calls that land within that window reuse the last-built
.agent-os/db/db_graph.json instead of performing a full rebuild.  Results
returned within the TTL window are therefore at most TTL seconds stale.

Set AGENT_OS_DB_GRAPH_TTL=0 (or any non-positive value) to restore
today's behaviour: every call unconditionally rebuilds the graph.

Return-value shape note: the tuple is (success: bool, msg: str | None).
On a happy rebuild or a cache hit, msg is None.  On a rebuild *failure*
when a cached file exists on disk, the function returns (True, <warning>)
— the caller can serve the last-good graph while surfacing the warning to
the user.  Only when no cached file is available does a failure return
(False, error).
"""

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


_DEFAULT_DB_GRAPH_TTL = 30  # seconds

# Where db_* tools build to, relative to the repo root.
_DB_OUTPUT_SUBDIR = (".agent-os", "db")

# Default pyodbc connection timeout (seconds) for the in-process build.
_DB_CONNECT_TIMEOUT = 30

# --- module-level pooled pyodbc connection ----------------------------------
# The DB graph is now built in-process (see refresh_database_graph): instead of
# spawning build_db_graph.py as a subprocess on every db_* call — paying a cold
# interpreter start plus a fresh DB auth handshake each time — we import the
# build core and reuse a single long-lived pyodbc connection across calls.
#
# The connection is opened lazily on first use and cached here.  A dead/closed
# connection (server restart, network blip, idle timeout) is detected and the
# handle transparently reopened on the next build, mirroring the per-operation
# resilience the card subsystem has for SQLite.  pyodbc stays OPTIONAL: it is
# imported only inside _get_db_connection, so the card and code-graph tools work
# with pyodbc absent.
_db_connection = None  # type: Optional[Any]
# Guards the (probe → close → reopen) sequence below. The MCP server is
# single-threaded today, so this is defense-in-depth: it prevents two callers
# from racing into _open_db_connection() and leaking a handle if the server is
# ever run multi-threaded.
_db_connection_lock = threading.Lock()


def _get_db_connection_string() -> Optional[str]:
    """Resolve the SQL Server connection string from .env / the environment.

    build_db_graph.py calls load_dotenv() at import time, which populates
    os.environ from a repo-local .env; we read DB_CONNECTION_STRING from there.
    Returns None when it is unset.
    """
    conn_str = os.environ.get("DB_CONNECTION_STRING")
    return conn_str if conn_str else None


def _connection_is_alive(conn: Any) -> bool:
    """Best-effort liveness probe for a pooled pyodbc connection.

    Runs a trivial ``SELECT 1``.  Any pyodbc error (or other exception) means the
    handle is dead/closed and must be reopened.
    """
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchall()
        cursor.close()
        return True
    except Exception:
        return False


def _open_db_connection():
    """Open a fresh pyodbc connection from DB_CONNECTION_STRING.

    Raises RuntimeError if pyodbc is not installed or the connection string is
    unset, and lets a pyodbc connection error propagate.
    """
    try:
        import pyodbc
    except ImportError as exc:
        raise RuntimeError(
            "pyodbc is not installed; the database-graph tools are unavailable"
        ) from exc

    conn_str = _get_db_connection_string()
    if not conn_str:
        raise RuntimeError(
            "DB_CONNECTION_STRING is not set (.env / environment); "
            "cannot build the database graph"
        )

    return pyodbc.connect(conn_str, timeout=_DB_CONNECT_TIMEOUT, autocommit=True)


def _get_db_connection():
    """Return the pooled pyodbc connection, opening or reconnecting as needed.

    Reuses the cached module-level handle across calls.  If the cached handle is
    dead (failed liveness probe), it is closed and a new one opened — so a single
    reconnect happens transparently on the next build after a drop.
    """
    global _db_connection

    with _db_connection_lock:
        if _db_connection is not None and _connection_is_alive(_db_connection):
            return _db_connection

        # Either no connection yet, or the cached one is dead: (re)open.
        if _db_connection is not None:
            try:
                _db_connection.close()
            except Exception:
                pass
            _db_connection = None

        _db_connection = _open_db_connection()
        return _db_connection


def _reset_db_connection() -> None:
    """Drop the pooled connection (close + clear). Used on hard build failures."""
    global _db_connection
    if _db_connection is not None:
        try:
            _db_connection.close()
        except Exception:
            pass
    _db_connection = None


def _get_db_graph_ttl() -> float:
    """Read AGENT_OS_DB_GRAPH_TTL from the environment.

    Returns the TTL in seconds (float).  Non-positive values mean no cache
    (always rebuild).  On parse failure the default is used.
    """
    raw = os.environ.get("AGENT_OS_DB_GRAPH_TTL", "")
    if raw.strip() == "":
        return float(_DEFAULT_DB_GRAPH_TTL)
    try:
        return float(raw)
    except (ValueError, TypeError):
        return float(_DEFAULT_DB_GRAPH_TTL)


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


def _import_build_core():
    """Import the importable core from db_tools/build_db_graph.py.

    Adds the db_tools directory to sys.path (the build scripts live there and
    import each other by bare name) and returns the module.  Raises RuntimeError
    if the build scripts cannot be located.
    """
    db_tools = find_db_tools_dir()
    if db_tools is None:
        raise RuntimeError(
            "Graph build scripts not found (build_db_graph.py)"
        )
    db_tools_str = str(db_tools)
    if db_tools_str not in sys.path:
        sys.path.insert(0, db_tools_str)
    import build_db_graph  # type: ignore
    return build_db_graph


def _build_db_graph_in_process(repo_root: Path, include_html: bool = False) -> None:
    """Build the database graph in-process using the pooled pyodbc connection.

    Writes db_graph.json / .md / .graphml under <repo_root>/.agent-os/db using
    the importable core (build_and_write).  This is the data-only hot path for
    db_* tool queries — it never builds the pyvis HTML (card #2); the Flask UI
    refresh endpoint produces the HTML separately.  ``include_html`` is accepted
    for API symmetry but HTML stays out of the tool path.

    Reuses the long-lived connection from _get_db_connection().  On a pyodbc
    error the pooled connection is reset (so the *next* call reconnects) and the
    error is re-raised for the caller's failure-resilience wrapper to handle.

    Missing optional dependencies (networkx / pyodbc) surface from the build core
    as SystemExit; here they are converted to a RuntimeError so the MCP tool path
    degrades gracefully through refresh_database_graph's ``except Exception``
    wrapper instead of tearing down the server process.
    """
    build_core = _import_build_core()
    output_dir = repo_root.joinpath(*_DB_OUTPUT_SUBDIR)

    try:
        import pyodbc
        db_errors: tuple = (pyodbc.Error,)
    except ImportError:
        db_errors = ()

    try:
        conn = _get_db_connection()
        build_core.build_and_write(conn, output_dir, include_html=include_html)
    except db_errors:
        # A live-connection failure mid-build: drop the pooled handle so the next
        # refresh reconnects, then surface the error to the resilience wrapper.
        _reset_db_connection()
        raise
    except SystemExit as exc:
        # build_db_graph raises SystemExit when networkx/pyodbc are missing.
        # SystemExit is not an Exception, so it would bypass the caller's
        # graceful-degradation handler — translate it.
        raise RuntimeError(str(exc) or "database graph build dependency missing") from exc


def build_targeted_database_graph(
    entry_point: str,
    entry_type: str,
    max_depth: int = 1,
) -> Optional[Dict[str, Any]]:
    """Build a BOUNDED-neighborhood database graph IN MEMORY and return the dict.

    This is the targeted-build counterpart to refresh_database_graph().  Unlike
    the full build, it is **in-memory only**:

      * it does NOT write db_graph.json / .md / .graphml,
      * it does NOT read or update the TTL cache,
      * it does NOT touch the shared full-graph file.

    It scopes the build to the ``max_depth``-hop neighborhood around
    ``entry_point`` (resolved by ``entry_type`` in {table, column, function,
    procedure}) over the pooled pyodbc connection (the same long-lived handle the
    full in-process build reuses) and RETURNS the graph dict directly to the
    caller.  The node/edge schema is identical to the full build — the result is
    simply a subset — so callers can run the existing tool matching logic on it.

    The single TTL-cached db_graph.json remains exclusively for full builds (the
    Flask UI, graph_refresh, and the global db_* tools); a scoped graph never
    shares that cache file.

    pyodbc stays OPTIONAL (imported only inside the connection pool).  Returns
    None on any failure — a missing dependency, an unset connection string, or a
    live-connection error — so the targeted db_* tools degrade gracefully (their
    existing "graph not found" branch) instead of crashing.  On a pyodbc error
    the pooled handle is reset so the next call reconnects.
    """
    try:
        import pyodbc
        db_errors: tuple = (pyodbc.Error,)
    except ImportError:
        db_errors = ()

    try:
        build_core = _import_build_core()
        conn = _get_db_connection()
        return build_core.build_targeted_graph_data(
            conn,
            entry_point=entry_point,
            entry_type=entry_type,
            max_depth=max_depth,
        )
    except db_errors:
        # Live-connection failure mid-build: drop the pooled handle so the next
        # call reconnects, and degrade gracefully (no shared cache to fall back
        # on for a scoped build).
        _reset_db_connection()
        log(f"ERROR building targeted database graph for {entry_point!r}: connection error")
        return None
    except SystemExit as exc:
        # build_db_graph raises SystemExit when networkx/pyodbc are missing.
        # SystemExit is not an Exception, so it would bypass the handler below —
        # treat a missing build dependency as a graceful failure (return None).
        log(
            f"ERROR building targeted database graph for {entry_point!r}: "
            f"{exc or 'database graph build dependency missing'}"
        )
        return None
    except Exception as exc:
        # _import_build_core RuntimeError (scripts missing), unset connection
        # string, malformed entry point, etc.  Targeted builds have no cache
        # fallback, so surface None and let the caller report "graph not found".
        log(f"ERROR building targeted database graph for {entry_point!r}: {exc}")
        return None


def refresh_database_graph(include_html: bool = False) -> tuple[bool, Optional[str]]:
    """Rebuild the database graph in-process and return (success, msg).

    The graph is built INSIDE this process (no subprocess spawn) by importing
    the build core from build_db_graph.py and reusing a module-level pooled
    pyodbc connection (see _get_db_connection).  By default this builds DATA ONLY
    — db_graph.json / .md / .graphml — and does NOT generate the pyvis HTML; the
    Flask UI refresh endpoint produces the HTML separately.  ``include_html`` is
    accepted for API symmetry; the tool path leaves it False.

    Cache behaviour (TTL guard)
    ---------------------------
    If AGENT_OS_DB_GRAPH_TTL > 0 (default 30 s) and db_graph.json exists
    with an mtime younger than the TTL, this function is a no-op and
    returns (True, None) immediately — no rebuild is performed.

    Failure resilience
    ------------------
    If a rebuild is attempted and fails but db_graph.json already exists on
    disk, this returns (True, <warning>) so callers can serve the last-good
    graph.  Only when no cached file is present does failure return
    (False, error_msg).  pyodbc being absent is treated as a build failure, so
    the db_* tools degrade gracefully while the card/code-graph tools are
    unaffected.
    """
    repo_root = get_repo_root()
    graph_path = repo_root / ".agent-os" / "db" / "db_graph.json"

    # --- TTL guard (cache hit check) ---
    ttl = _get_db_graph_ttl()
    if ttl > 0 and graph_path.exists():
        try:
            age = time.time() - graph_path.stat().st_mtime
            if age < ttl:
                log(f"DB graph cache hit (age={age:.1f}s, TTL={ttl}s); skipping rebuild")
                return True, None
        except OSError:
            # If we can't stat the file, fall through to rebuild
            pass

    # --- Attempt rebuild (in-process, pooled connection) ---
    try:
        db_tools = find_db_tools_dir()
        if db_tools is None:
            error_msg = "Graph build scripts not found (build_db_graph.py)"
            log(f"ERROR refreshing database graph: {error_msg}")
            if graph_path.exists():
                warning = f"DB graph rebuild skipped (tools missing); serving cached graph. {error_msg}"
                log(f"WARNING: {warning}")
                return True, warning
            return False, error_msg

        # Build DATA ONLY in-process. No subprocess, no pyvis HTML.
        _build_db_graph_in_process(repo_root, include_html=include_html)
        log("Database graph rebuilt in-process")

        return True, None
    except Exception as e:
        # Log the full error to stderr, but only surface controlled messages to
        # callers. A RuntimeError here comes from _open_db_connection (missing
        # pyodbc / unset DB_CONNECTION_STRING) and is safe to show; any other
        # exception text (file paths, module names) is generalized to avoid
        # internal disclosure in tool responses — the same leak class fixed for
        # the Flask UI in 0.1.14.
        log(f"ERROR refreshing database graph: {e}")
        safe_msg = str(e) if isinstance(e, RuntimeError) else "Internal build error; see server logs"
        if graph_path.exists():
            warning = f"DB graph rebuild failed; serving cached graph. {safe_msg}"
            log(f"WARNING: {warning}")
            return True, warning
        return False, safe_msg


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


def _normalize_code_graph(graph: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a graphify NetworkX node-link graph to the shape the tools read.

    graphify emits NetworkX node-link JSON, where edges live under a top-level
    ``"links"`` key, each edge labels its kind under ``"relation"``, and each
    node describes its kind under ``"file_type"``. The code-graph tools, however,
    read ``"edges"`` / ``edge["relationship"]`` / ``node["type"]``. Without this
    bridge every traversal and filter silently returns empty.

    The normalization is **additive, idempotent, and non-destructive**: it only
    fills the alias keys when they are absent, and never removes the originals.
    A graph already in ``edges`` / ``relationship`` / ``type`` form (e.g. an
    already-normalized graph or the database graph shape) passes through
    unchanged.
    """
    if not isinstance(graph, dict):
        return graph

    # links -> edges (top-level)
    if "edges" not in graph and "links" in graph:
        graph["edges"] = graph["links"]

    # relation -> relationship (per edge)
    for edge in graph.get("edges", []) or []:
        if isinstance(edge, dict) and "relationship" not in edge and "relation" in edge:
            edge["relationship"] = edge["relation"]

    # file_type -> type (per node)
    for node in graph.get("nodes", []) or []:
        if isinstance(node, dict) and "type" not in node and "file_type" in node:
            node["type"] = node["file_type"]

    return graph


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
            return _normalize_code_graph(json.load(f))
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
