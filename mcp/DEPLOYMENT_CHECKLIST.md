# Deployment Checklist - Move to Plugins Folder

## Pre-Deployment Verification ✅

### Code Quality
- [x] Python syntax valid (verified with py_compile)
- [x] All unit tests passing (8/8)
- [x] No linting errors (imports clean, standard library usage)
- [x] No hardcoded absolute paths
- [x] No relative path traversals

### Documentation
- [x] README.md - Quick start guide present
- [x] CLAUDE.md - Comprehensive tool documentation
- [x] INTEGRATION.md - Setup instructions
- [x] PROJECT_SUMMARY.md - Complete feature overview
- [x] FIXES_APPLIED.md - Change documentation
- [x] This checklist - Deployment verification

### Dependencies
- [x] requirements.txt created with mcp, fastmcp
- [x] All imports verified (FastMCP loads correctly)
- [x] No missing dependencies

### Portability
- [x] Repo discovery via .git (auto-detects root)
- [x] .agent-os created at repo root (auto-isolated)
- [x] Path resolution handles multiple layouts
- [x] Works with any project structure

### Error Handling & Observability
- [x] Graph server health monitoring
- [x] Startup failure detection & logging
- [x] Graceful degradation when optional services unavailable
- [x] Comprehensive error messages for debugging

---

## Plugin Layout

This is a Claude Code plugin. The full plugin root contains:

```
agent-os/
├── .claude-plugin/plugin.json   ← Plugin manifest (required)
├── .mcp.json                    ← MCP server config (uses ${CLAUDE_PLUGIN_ROOT})
├── hooks/hooks.json             ← Graph-sync + file-protection hooks
├── scripts/                     ← Hook implementations
├── agents/                      ← 5 delegation agents
├── skills/                      ← 17 workflow skills
└── mcp/
    ├── server.py                ← MCP server (cards + 18 graph tools)
    ├── test_server.py           ← Card test suite (8/8 passing)
    ├── requirements.txt         ← Dependencies
    └── db_tools/                ← Optional SQL Server graph builder
        ├── app.py
        ├── build_db_graph.py
        └── build_graph_html.py
```

---

## Installation Instructions for Users

### Step 1: Install dependencies
```bash
pip install -r mcp/requirements.txt
```

### Step 2: Load the plugin in Claude Code
```bash
claude --plugin-dir /path/to/agent-os
```
The manifest, MCP server, hooks, agents, and skills are auto-discovered. Because
`.mcp.json` uses `${CLAUDE_PLUGIN_ROOT}`, no path editing is required.

### Step 3: Verify
- `claude plugin validate /path/to/agent-os` should pass.
- The `task-cards` MCP tools should appear in the MCP tools list.

### Step 4: Start Using
```python
# In Claude Code, agents can now use:
create_card(title="...", priority="high")
list_cards(status="In Progress")
get_card(card_id)
# ... etc
```

---

## Verification After Deployment

After moving to plugins folder, verify:

1. **Dependencies Install Cleanly**
   ```bash
   pip install -r requirements.txt  # Should complete without errors
   ```

2. **Server Starts Without Errors**
   ```bash
   python3 server.py  # Should initialize database and start cleanly
   ```

3. **Tests Pass in New Location**
   ```bash
   python3 test_server.py  # All 8 tests should pass
   ```

4. **Repo Discovery Works**
   - Server correctly finds .git and creates .agent-os
   - Database created at repo root/.agent-os/cards.sqlite
   - Cards are per-repository

5. **Tools Are Available**
   - In Claude Code: task-cards tools appear in MCP tools list
   - All 6 card management tools functional
   - Graph tools functional (if graphify/db_tools available)

---

## Rollback Plan

If issues arise after deployment:

1. **Quick Fix**: Update just `server.py` without reinstalling
2. **Rollback**: Remove plugin folder: `rm -rf ~/.claude/plugins/task-cards`
3. **Check Logs**: Look for error messages in Claude Code logs
4. **Report**: Create issue with error output and logs

---

## Success Criteria ✅

- [x] Plugin structure is self-contained
- [x] No external dependencies beyond mcp/fastmcp
- [x] Works with existing Claude Code setup
- [x] Repository-local storage (not shared/global)
- [x] All tests pass without modification
- [x] Documentation is complete
- [x] Error messages are user-friendly
- [x] Setup is one-time per project

---

## Status

### Card system: ready. Database graph: optional, see caveats.

- The **card system** (6 tools) is functional and tested (8/8), and the plugin
  loads and passes `claude plugin validate`.
- The **database-graph subsystem** is optional: it requires a live SQL Server and
  a configured `.env`, and it refreshes synchronously (can block up to ~60s per
  `db_*` call). See `FIXES_APPLIED.md` (2026-06-18) for the current change history
  and known limitations.

