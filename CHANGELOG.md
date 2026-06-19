# Changelog

All notable changes to the **agent-os** plugin are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.3] - 2026-06-18

### Fixed
- Agents could not load their wired skills: the scoped `tools:` allowlists added
  in 0.1.2 omitted the `Skill` tool, so the "load these skills" instructions in
  the agent bodies were inert. Added `Skill` to all five agents' tool lists.
  (Found by running the read-only `code-reviewer` agent as a live config check.)

## [0.1.2] - 2026-06-18

### Added
- Agent tool-scoping: each delegation agent now declares an explicit `tools:`
  allowlist instead of inheriting every tool. Read-only agents (code-reviewer,
  research-planner) no longer have Edit/Write; no subagent has
  `complete_card`/`graph_refresh` (lead-only).
- Skill wiring: every agent body now loads its preload skills via the Skill tool
  and restates its hard guardrails.
- `AGENT_OS_GRAPHIFY_ARGS` env var to pass extra flags (e.g. a backend) to the
  graph-sync `graphify` invocation.

### Changed
- Agent models: `implementer`, `research-planner`, and `test-engineer` promoted
  from `haiku` to `sonnet` (the cheapest tier was doing the hardest work).
- `requirements-to-cards` and `test-execution-reporting` skills now name the exact
  MCP tool IDs they depend on.
- Documentation overhaul: canonical marketplace install across README /
  INTEGRATION / DEPLOYMENT_CHECKLIST; `templates/CLAUDE.md` made portable
  (database-graph section gated optional, lead model de-named, plugin skills
  referenced, trimmed ~11KB→~7KB); documented the graph UI + `AGENT_OS_GRAPH_PORT`,
  artifact locations, the `graphify` dependency, and stderr logging.

### Fixed
- Graph-sync hooks no longer fail on every event: `refresh_graphify` treats the
  "no LLM API key" case as a clean no-op instead of emitting a failure banner.

### Removed
- Dev-scratch docs `outline.md` and `mcp/FIXES_APPLIED.md` (history now lives in
  this changelog).

## [0.1.1] - 2026-06-18

### Added
- `.claude-plugin/marketplace.json` so the plugin can be installed natively via
  `/plugin marketplace add` + `/plugin install agent-os@agent-os-local`.
- `.claude-plugin/plugin.json` — the required plugin manifest that was previously
  missing, so the package could not load as a proper Claude Code plugin.
- `.env.example` documenting the optional `DB_CONNECTION_STRING` for the
  database-graph subsystem.
- Configurable graph UI port via `AGENT_OS_GRAPH_PORT` (default 5000); the server
  reuses an already-running graph instance instead of starting a duplicate.

### Fixed
- **MCP stdio logging**: the server now logs to stderr so diagnostic output no
  longer corrupts the stdio MCP protocol stream.
- **Database graph build location**: `refresh_database_graph()` previously ran
  `build_db_graph.py` / `build_graph_html.py` from the repo root, but they live in
  `mcp/db_tools/`, so every `db_*` tool failed with `CalledProcessError`. Added
  `find_db_tools_dir()` to locate the scripts next to `server.py` (with fallbacks).
- **`db_tools/app.py`** referenced a nonexistent `visualize_db_graph.py` and used
  the wrong base directory for the build scripts; corrected to
  `db_tools/build_graph_html.py`.
- **`.mcp.json` portability**: replaced hardcoded `.venv` absolute paths with
  `python3` + `${CLAUDE_PLUGIN_ROOT}/mcp/server.py`.
- **`update_card`** now validates `status` against `Created` / `In Progress` /
  `Complete` instead of accepting any string.
- **`scripts/sync_repo_graph.py`** no longer emits the health JSON twice on
  `SessionStart`.
- **`hooks/hooks.json`** uses `python3` instead of bare `python`.

### Changed
- `requirements.txt` ships `mcp` only; the unused `fastmcp` dependency was
  removed. Database-graph dependencies (`pyodbc` + live SQL Server) are optional.

### Known Limitations
- `db_*` tools refresh the graph synchronously (up to ~60s), blocking the event
  loop. The database subsystem also requires a live SQL Server + `.env`.
- `datetime.utcnow()` is still used (deprecated but functional on 3.12).

## [0.1.0] - 2026-06-16

### Added
- Graph server subprocess error reporting: switched Flask graph-server output from
  `DEVNULL` to `PIPE`, added `_check_graph_server_health()` to detect unexpected
  process exits and surface stdout/stderr, and wired health checks into
  `load_database_graph()` and `load_code_graph()`.
- Multi-path resolution for the database tools' Flask app, checking
  `mcp/db_tools/app.py` and `db_tools/app.py`, with clear logging of searched
  paths when the app is missing.

### Documented
- Explained why `check_same_thread=False` is safe for the SQLite connection: the
  MCP server runs in a single-threaded event loop and FastMCP serializes all tool
  calls, so concurrent access cannot occur.

### Notes
- A later review (see 0.1.1) found the original "no hardcoded paths" /
  "production-ready" claims from this round were premature; several real defects
  remained and were fixed in 0.1.1.
