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

## Files Ready for Transfer

```
mcp/
├── server.py                    ← Main MCP server (FIXED: 3 issues resolved)
├── test_server.py              ← Test suite (VERIFIED: 8/8 passing)
├── example_usage.py            ← Usage examples
├── setup-mcp.sh                ← Integration helper
├── requirements.txt            ← Dependencies (NEW: created)
├── db_tools/                   ← Graph server
│   ├── app.py
│   ├── build_db_graph.py
│   └── build_graph_html.py
├── README.md                   ← Quick start
├── CLAUDE.md                   ← Full docs
├── INTEGRATION.md              ← Setup guide
├── PROJECT_SUMMARY.md          ← Feature overview
├── FIXES_APPLIED.md            ← Change log (NEW: created)
└── DEPLOYMENT_CHECKLIST.md     ← This file (NEW: created)
```

---

## Installation Instructions for Users

### Step 1: Install to Plugins Folder
```bash
# Copy to your Claude Code plugins folder
cp -r mcp ~/.claude/plugins/task-cards

# Install dependencies
cd ~/.claude/plugins/task-cards
pip install -r requirements.txt
```

### Step 2: Enable in Project
```bash
cd /path/to/your/project
~/.claude/plugins/task-cards/setup-mcp.sh
```

### Step 3: Restart Claude Code
- Restart Claude Code in the project directory
- Task cards tools will be automatically available

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

### READY FOR PRODUCTION ✅

This plugin is ready to be moved to the plugins folder with confidence. All three critical issues have been resolved, all tests pass, and deployment is straightforward.

