# Deployment Checklist

## Pre-Deployment Verification

### Code Quality
- [x] Python syntax valid (verified with `py_compile`)
- [x] Card unit tests passing (8/8)
- [x] No hardcoded absolute paths
- [x] `.mcp.json` uses `${CLAUDE_PLUGIN_ROOT}` (portable)

### Documentation
- [x] `README.md` — install and overview
- [x] `CLAUDE.md` — tool documentation
- [x] `PROJECT_SUMMARY.md` — card subsystem summary
- [x] `CHANGELOG.md` — version history
- [x] This checklist — deployment verification

### Dependencies
- [x] `mcp/requirements.txt` ships `mcp` only
- [x] Database-graph subsystem deps (`pyodbc` + live SQL Server + `.env`
      `DB_CONNECTION_STRING`) are optional

### Portability
- [x] Repo discovery via `.git` (auto-detects root)
- [x] `.agent-os/` created at repo root (auto-isolated)
- [x] Build scripts located at runtime via `find_db_tools_dir()`

### Error Handling & Observability
- [x] MCP server logs to stderr (does not corrupt the stdio protocol)
- [x] Graph server health monitoring
- [x] Startup failure detection & logging
- [x] Graceful degradation when optional services unavailable

---

## Plugin Layout

This is a Claude Code plugin (`agent-os`). The full plugin root contains:

```
agent-os/
├── .claude-plugin/
│   ├── plugin.json              ← Plugin manifest (required)
│   └── marketplace.json         ← Local marketplace definition
├── .mcp.json                    ← MCP server config (uses ${CLAUDE_PLUGIN_ROOT})
├── hooks/
│   ├── hooks.json               ← Graph-sync + file-protection hooks
│   └── README.md
├── scripts/                     ← Hook implementations
├── agents/                      ← 5 delegation agents
├── skills/                      ← 17 workflow skills
├── templates/                   ← Card / workflow templates
├── installer/                   ← Setup helpers (install.sh, etc.)
└── mcp/
    ├── server.py                ← MCP server (cards + 18 graph tools)
    ├── test_server.py           ← Card test suite (8/8 passing)
    ├── requirements.txt         ← Dependencies (mcp only)
    ├── example_usage.py         ← Usage examples
    └── db_tools/                ← Optional SQL Server graph builder
        ├── app.py               ← Flask graph UI (port 5000, AGENT_OS_GRAPH_PORT)
        ├── build_db_graph.py
        └── build_graph_html.py
```

---

## Installation Instructions for Users

### Step 1: Install dependencies
```bash
pip install -r mcp/requirements.txt
```

### Step 2: Install the plugin in Claude Code
```bash
/plugin marketplace add /path/to/agent-os
/plugin install agent-os@agent-os-local
```
The manifest, MCP server, hooks, agents, and skills are auto-discovered. Because
`.mcp.json` uses `${CLAUDE_PLUGIN_ROOT}`, no path editing is required.

(`claude --plugin-dir /path/to/agent-os` is a dev-only alternative.)

### Step 3: Verify
- The `task-cards` MCP tools should appear in the MCP tools list.
- All 6 card management tools should be functional.

---

## Releasing Changes (version-bump discipline)

**The plugin cache is keyed by version.** When you change any packaged file that
affects runtime — agent and skill definitions (`agents/`, `skills/`), hooks
(`hooks/hooks.json`, `scripts/`), the MCP server and graph tooling (`mcp/*.py`,
`mcp/db_tools/`, `mcp/requirements.txt`), or the manifests (`.claude-plugin/`,
`.mcp.json`) — you **must** bump `version` in `.claude-plugin/plugin.json` in the
same change. Documentation-only changes (`*.md`, including the docs under `mcp/`)
do not require a bump, since they don't affect the running plugin.

If you don't, `/plugin install` sees the unchanged version, reports *"already at
the latest version"*, and keeps serving the **stale cached copy** — your changes
never reach the running plugin even after `/reload-plugins`.

To ship a change:
1. Edit the files, **and** bump `.claude-plugin/plugin.json` `version` (+ add a
   `CHANGELOG.md` entry).
2. Refresh the install:
   ```bash
   /plugin marketplace update agent-os-local
   /plugin install agent-os@agent-os-local
   /reload-plugins
   ```
3. Confirm the cache updated — a new version dir should exist under
   `~/.claude/plugins/cache/agent-os-local/agent-os/<version>/`.

For tight iteration without bumping each time, use
`claude --plugin-dir /path/to/agent-os`, which live-loads from the source tree.

---

## Verification After Deployment

1. **Dependencies Install Cleanly**
   ```bash
   pip install -r mcp/requirements.txt
   ```

2. **Server Starts Without Errors**
   ```bash
   python3 mcp/server.py   # initializes database, logs to stderr, serves over stdio
   ```

3. **Tests Pass**
   ```bash
   python3 mcp/test_server.py   # all 8 card tests should pass
   ```

4. **Repo Discovery Works**
   - Server finds `.git` and creates `.agent-os/`
   - Database created at `<repo root>/.agent-os/cards.sqlite`
   - Cards are per-repository

5. **Tools Are Available**
   - `task-cards` tools appear in the MCP tools list
   - All 6 card management tools functional
   - Graph tools functional (if graphify / db_tools available)

---

## Rollback Plan

If issues arise after deployment:

1. **Quick Fix**: Editing the *source* tree requires a version bump + reinstall to
   reach the running plugin (see "Releasing Changes" above); only edits to the
   installed cache copy under `~/.claude/plugins/cache/.../<version>/` take effect
   without a reinstall.
2. **Rollback**: `/plugin uninstall agent-os@agent-os-local`
3. **Check Logs**: Look for the server's stderr output in Claude Code logs.
4. **Report**: Open an issue with the error output and logs.

---

## Status

### Card system: ready. Database graph: optional, see caveats.

- The **card system** (6 tools) is functional and tested (8/8), and the plugin
  loads and passes `claude plugin validate`.
- The **database-graph subsystem** is optional: it requires a live SQL Server and
  a configured `.env`, and it refreshes synchronously (can block up to ~60s per
  `db_*` call). See `CHANGELOG.md` for the current change history and known
  limitations.
