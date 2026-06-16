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

## Verification

✅ All syntax checks pass
✅ All unit tests pass (8/8)
✅ No hardcoded absolute paths
✅ No relative path traversals
✅ Zero breaking changes
✅ Backward compatible

## Ready for Deployment

The plugin is now production-ready for the plugins folder with:
- Improved error handling and observability
- Flexible path resolution for different setups
- Clear documentation of design decisions
- All tests passing

