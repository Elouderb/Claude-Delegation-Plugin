# Plugin Fixes Applied

## Date: 2026-06-16

This document summarizes the three critical fixes applied to make the Task Cards MCP Server production-ready for the plugins folder.

---

## Issue #1: Graph Server Subprocess Error Reporting

**Problem:**
- Flask graph server started with `stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL`
- Silent failures made debugging impossible if the server crashed on startup
- No visibility into why the graph server wasn't available

**Fix Applied:**
- Changed subprocess output handling from `DEVNULL` to `PIPE` for error capture
- Added `_check_graph_server_health()` function that:
  - Detects when Flask process exits unexpectedly
  - Logs stdout/stderr output for diagnosis
  - Warns user when graph server crashes
- Integrated health checks into `load_database_graph()` and `load_code_graph()`

**Impact:**
- Better observability when graph server fails
- Easier troubleshooting for plugin users
- No silent failures

**Code Location:** `mcp/server.py` lines 103-150

---

## Issue #2: Database Tools Path Resolution

**Problem:**
- Hard-coded path: `repo_root / "db_tools" / "app.py"`
- Actual location: `repo_root / "mcp" / "db_tools" / "app.py"`
- Would fail silently with warning instead of finding the correct location

**Fix Applied:**
- Implemented multi-path resolution that checks:
  1. `mcp/db_tools/app.py` (installed under mcp directory)
  2. `db_tools/app.py` (installed at repo root)
- Logs all searched paths when app not found
- Flexible for different installation layouts

**Impact:**
- Plugin works regardless of installation directory structure
- Clear error messages if Flask app is missing

**Code Location:** `mcp/server.py` lines 103-140

---

## Issue #3: Database Connection Thread Safety Documentation

**Problem:**
- Used `check_same_thread=False` with sqlite3 without explanation
- Could be misunderstood as unsafe in multi-threaded context
- Future maintainers might remove it without understanding why

**Fix Applied:**
- Added detailed comment explaining:
  - Why it's safe: MCP runs in single-threaded event loop
  - FastMCP serializes all tool calls
  - Concurrent access is not possible
  - Rationale for the decision

**Impact:**
- Clear documentation for future maintainers
- Reduces risk of accidental refactoring
- Demonstrates defensive programming practices

**Code Location:** `mcp/server.py` lines 63-67

---

> **Note (2026-06-18):** A later full review found that the "no hardcoded paths"
> and "production-ready" claims below were premature — several real defects
> remained (see the 2026-06-18 section). Treat the checkmarks below as scoped to
> the three issues in this 2026-06-16 round only.

## Verification (2026-06-16 round)

- All syntax checks pass
- All card unit tests pass (8/8)
- Zero breaking changes to the card tools

---

## 2026-06-18 Review Fixes

A full plugin review surfaced and fixed the following:

### Packaging
- **Added `.claude-plugin/plugin.json`** — the required plugin manifest was
  missing, so the package could not load as a proper Claude Code plugin.
- **`.mcp.json` portability** — replaced hardcoded `.venv` absolute paths with
  `python3` + `${CLAUDE_PLUGIN_ROOT}/mcp/server.py`.
- **Secrets** — added `.env.example`; confirmed the real `.env` is gitignored and
  was never committed.

### Database graph (was fully broken)
- **`server.py refresh_database_graph()`** ran `build_db_graph.py` /
  `build_graph_html.py` from the repo root, but they live in `mcp/db_tools/`.
  Every `db_*` tool failed with `CalledProcessError`. Added `find_db_tools_dir()`
  to locate the scripts next to `server.py` (with fallbacks). The earlier
  "Issue #2" fix patched `start_graph_server()` but missed this function.
- **`db_tools/app.py`** referenced a nonexistent `visualize_db_graph.py` and used
  the wrong base directory for the build scripts. Corrected to
  `db_tools/build_graph_html.py`.

### Correctness / portability
- **`update_card`** now validates `status` against `Created / In Progress /
  Complete` instead of accepting any string.
- **`scripts/sync_repo_graph.py`** no longer emits the health JSON twice on
  `SessionStart`.
- **`hooks/hooks.json`** uses `python3` instead of bare `python`.

### Verification
- Card unit tests pass (8/8), all Python compiles, all JSON validates, and
  `claude plugin validate .` passes.

### Known remaining limitations
- `db_*` tools refresh the graph synchronously (up to ~60s), blocking the event
  loop. The database subsystem also requires a live SQL Server + `.env`.
- `datetime.utcnow()` is still used (deprecated but functional on 3.12).

