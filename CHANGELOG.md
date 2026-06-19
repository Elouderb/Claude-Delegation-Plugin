# Changelog

All notable changes to the **agent-os** plugin are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.7] - 2026-06-19

### Fixed
- **Code graph never refreshed (blocker):** All three `graphify` refresh call
  sites — `graphify_command()` in `scripts/hook_common.py`, the `graph_refresh`
  MCP tool in `mcp/server.py`, and the `/<slug>/refresh/repo` route in
  `mcp/db_tools/app.py` — invoked `graphify . --update`. The graphify CLI uses
  subcommand syntax (`graphify update <path>`), so `.` was parsed as the command
  name. That form fell into the default semantic-extraction path, which errors
  out with "no LLM API key" and never rebuilds `graph.json`; because
  `refresh_graphify()` treats the no-key message as a clean no-op, the code graph
  silently never updated. Corrected to `graphify update .` (code-only rebuild, no
  LLM key required). The 0.1.5 changelog's `["graphify", ".", "--update"]` "fix"
  standardized every site onto this broken form; this reverses it.
- **Graph UI never started (blocker):** `flask` and `python-dotenv` were listed
  as *optional* dependencies bundled with the SQL-Server db subsystem, but
  `mcp/db_tools/app.py` imports both at module level and serves the code graph
  and task cards — core features. The MCP server spawns this app on startup, so
  with flask absent it crashed on `import flask` every boot and never bound its
  port. Promoted `flask` + `python-dotenv` to core requirements in
  `mcp/requirements.txt`.
- **Docs:** `README.md` and `hooks/README.md` updated to show `graphify update .`.

## [0.1.6] - 2026-06-19

### Added
- **Multi-repo graph UI:** `mcp/db_tools/app.py` now supports multiple concurrent
  Claude Code instances. Each MCP instance registers its repo root in
  `~/.agent-os/active_repos.json` on startup (`_register_repo()` in `mcp/server.py`).
  The Flask server reads this registry on every request and exposes a per-repo URL
  namespace: `/<slug>/code_graph`, `/<slug>/db_graph`, `/<slug>/task_cards`.
  The home page (`/`) lists all registered repos; repo slugs are derived from the
  directory name with a parent-dir prefix on collision.
- **Task cards web view:** `/<slug>/task_cards` renders a simple HTML table of cards
  read directly from `.agent-os/cards.sqlite` via `sqlite3` — no ORM needed.

### Fixed
- **XSS in graph UI (HIGH):** `_slug_not_found` and `_missing_file` now escape the
  `slug` and `msg` arguments with `markupsafe.escape` before interpolating into HTML.
- **XSS in graph UI (MEDIUM):** `repo_index` and `task_cards` routes now escape the
  `slug` for HTML text/title contexts (`escape(slug)`) and URL-encode it for href
  and JS `fetch()` string contexts (`urllib.parse.quote(slug, safe="")`). Slugs
  appearing in the home-page repo list (`index`) are also escaped.
- **Stored XSS in task_cards (MEDIUM):** All database-sourced values (`card_id`,
  `title`, `status`, `priority`, `updated_at`) are now escaped with
  `markupsafe.escape` before being interpolated into the HTML table.
- **Exception info disclosure in task_cards:** The bare `{exc}` interpolation in the
  SQLite error handler is replaced with a generic message; the exception is logged to
  `stderr` instead.
- **Cross-repo `.env` isolation:** `refresh_db` now uses `dotenv_values()` (returns
  a dict; no side effects on `os.environ`) and passes the connection string explicitly
  to the subprocess, preventing `.env` from one repo leaking into another.

## [0.1.5] - 2026-06-19

### Fixed
- **Graph server path (blocker):** `start_graph_server()` in `mcp/server.py` now
  locates `app.py` plugin-relative (`Path(__file__).resolve().parent / "db_tools" /
  "app.py"`) instead of searching the user's repo root. Previously, the Flask graph
  UI would silently never start for any externally installed user.
- **Graphify refresh invocation (blocker):** Both the `graph_refresh` MCP tool in
  `mcp/server.py` and the `/refresh/repo` route in `mcp/db_tools/app.py` now call
  `["graphify", ".", "--update"]` (the `.` path argument was missing in both,
  inconsistent with `hook_common.py`'s `graphify_command()`).
- **Installer (blocker):** `installer/install.sh` post-install text now describes
  the canonical `/plugin marketplace add` + `/plugin install` + `/reload-plugins`
  flow instead of the obsolete manual MCP settings path.
- **`datetime.utcnow()` deprecation:** All four call sites in `server.py` (`create_card`,
  `update_card`, `add_comment`, `format_graph_response`) now use
  `datetime.now(timezone.utc)` (Python 3.12+).
- **`graph_search_nodes` truncation flag:** `truncated = len(nodes) > limit` corrected
  to `len(results) >= limit` — the old check tested the unfiltered node count, not
  whether the result set was actually capped.
- **`_graph_port()` `PORT` fallback removed:** The function no longer falls back to
  the `PORT` environment variable (claimed by Heroku, Railway, Render, etc.), which
  could silently redirect the graph UI to an unrelated port.
- **`mcp` version pin tightened:** `mcp>=0.1.0` → `mcp>=1.0,<2` (tested against
  1.27.1; prevents silent adoption of a hypothetical breaking 2.x release).
- **README version string:** First-paragraph version updated from `0.1.1` to `0.1.5`.

## [0.1.4] - 2026-06-18

### Fixed
- Graph UI (`mcp/db_tools/app.py`) now reads `.env` and graph data from the
  **local repository** instead of the plugin install directory. `BASE_DIR`
  (`Path(__file__).parent.parent`) resolved to the plugin's `mcp/` dir, so the
  Flask UI loaded `DB_CONNECTION_STRING` from the plugin's `.env` and looked for
  graphs under `<plugin>/mcp/.agent-os/db` / `graphify-out`. Split into
  `SCRIPT_DIR` (build-script location) and a cwd/`.git`-derived `REPO_ROOT` used
  for `.env`, the graph directories, and the refresh subprocess `cwd`. The `db_*`
  MCP build path (`build_db_graph.py`) was already repo-local; this aligns the UI.

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
