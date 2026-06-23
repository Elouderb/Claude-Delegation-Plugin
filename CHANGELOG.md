# Changelog

All notable changes to the **agent-os** plugin are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.7] - 2026-06-22

### Fixed
- **Card database is now protected from git-driven loss.** The live SQLite card
  store (`.agent-os/cards.sqlite`) lives in the working tree, and nothing made
  git ignore it while the docs said it was "safe to commit". In projects where
  the DB was committed, a `git reset --hard`, checkout, or rebase would rewind
  `cards.sqlite` to an older snapshot — silently dropping every card created
  after that commit. The plugin now drops a self-protecting `.agent-os/.gitignore`
  (single wildcard `*`) the first time either the MCP server (`ensure_agent_os()`,
  before any card can be created) or a hook (`state_dir()`) initialises
  `.agent-os/`, so git never tracks the DB. The write is idempotent (never
  clobbers an existing `.gitignore`) and best-effort (any `OSError` is swallowed,
  never breaking card or hook flows). `mcp/setup-mcp.sh` also creates it at setup.
  The plugin's lifecycle hooks were exonerated — they only *detect* git-state
  changes; they run no destructive git commands and never delete cards.

### Changed
- **Docs no longer advise committing the live card DB.** `CLAUDE.md` and
  `hooks/README.md` now explain that `.agent-os/` is auto-ignored, that committing
  the live SQLite file risks losing cards on `git reset`/checkout/rebase, and that
  `git clean -fdx` (the `-x` flag deletes ignored files) still removes
  `.agent-os/` — use `git clean -fd` or `-e .agent-os` to preserve it. Projects
  whose DB is already committed need a one-time `git rm -r --cached .agent-os/`.

### Added
- **`mcp/tests/test_gitignore_protection.py`** — covers the `.gitignore`
  helper's creation, idempotency, `OSError` swallowing, and invocation from both
  the server (`ensure_agent_os()`) and hook (`state_dir()`) chokepoints.

## [0.2.6] - 2026-06-22

### Added
- **Per-project live task-card board on the project page.** Each
  `/<slug>/` repo page now renders the same 3-column board (Created / In Progress
  / Complete) below the graph tiles, served by a new `GET /<slug>/task_cards.json`
  and polled every 4s — top-3-per-column with a per-project Expand/Shrink toggle
  (expand state survives the live polls), chips linking to the card detail page.
  The "View cards" table link is preserved.
- **`/all_cards` rows sorted by recent activity.** Project rows are ordered by
  each project's most-recent card update (`MAX(updated_at)`) descending, with
  no-activity projects last (slug tiebreak), so the most active projects rise to
  the top.

### Fixed
- **Tests no longer leak temp directories into `/tmp`.** `test_hook_common.py`
  and `test_code_graph_tools.py` created `tempfile.mkdtemp()` dirs without
  guaranteed teardown; they now use recognizable prefixes and `addCleanup` so the
  directories are removed even when a test fails.

### Security
- The slug is embedded into the per-project board's inline `<script>` as a JS
  string literal; since `json.dumps()` does not escape `/`, a registry slug
  containing `</script>` (e.g. a tampered `active_repos.json` or a malicious repo
  directory name) could break out of the script block. It is now escaped
  (`</` → `<\/`, the OWASP pattern) with a regression test. The
  `GET /<slug>/task_cards.json` route is read-only, escapes nothing into HTML
  (JSON only), 404s an unknown slug, and never 500s on a missing/corrupt DB.

## [0.2.5] - 2026-06-22

### Added
- **Cross-project "All Cards" board in the graph UI.** A new top-level
  `GET /all_cards` renders a Jira-style board across every registered repository:
  one row per project, three status columns (Created / In Progress / Complete),
  with compact card chips that each link to the existing card detail page. It is
  view-only and live — an inline script polls `GET /all_cards.json` every 4s
  (paused when the tab is hidden) and re-renders the grid in place, so chips move
  between columns as card statuses change with no manual refresh. The board is
  server-rendered first (works without JS), surfaces any non-canonical-status
  cards via a "+N other" badge without inflating the per-project count, and is
  linked from the index page. A missing or corrupt per-repo `cards.sqlite` shows
  empty columns / an inline error rather than failing the whole board.
- **Top-3-per-column with per-project Expand/Shrink.** Each column initially
  shows the 3 most-recently-created cards; an Expand button appears in the
  project's slug cell whenever any column holds more than 3 cards. Clicking
  toggles all columns open (Shrink to collapse). Expand state persists across
  the 4 s live polls — the JS `renderBoard` reads per-slug state from
  `expanded[slug]` so expanded projects never snap back. The static server render
  uses the same 3-card cap; the first JS poll replaces it promptly. Cards are
  ordered by `created_at DESC` (matching the per-project `/task_cards` view).
- **Prominent "All Cards board" button on the index page.** A styled
  `.board-link-btn` anchor now appears directly under the `<h1>` heading — above
  the repository list in both the populated and empty-registry states. The text
  link that was previously in the footer has been removed (Health remains there).

### Security
- All card data is XSS-safe on both render paths: the server `escape()`s every
  DB-sourced field and `urllib.parse.quote`s URL components (including the
  `data-slug` the poll script reconciles on), and the client builds DOM via
  `createElement` + `textContent` with `encodeURIComponent` hrefs and no
  `innerHTML`. The routes are read-only `GET`s with no user input reaching SQL or
  the filesystem.

## [0.2.4] - 2026-06-22

### Fixed
- **Graph server is reaped when its parent MCP server dies.** The Flask graph
  server (`mcp/db_tools/app.py`) was spawned with no OS-level lifetime coupling,
  and the only cleanup (`server.py`'s `atexit` handler) runs only on a clean
  Python exit — so `SIGKILL`, default-disposition `SIGTERM`, a crash, or the
  parent being force-reaped all orphaned the child on `:5000` (reparented to PID
  1). On Linux the child is now coupled to its parent via
  `prctl(PR_SET_PDEATHSIG, SIGTERM)` set in a `Popen` `preexec_fn`, with an
  `os.getppid()` re-check to close the fork/parent-death race. It is attached
  only when spawning from the main thread (`PR_SET_PDEATHSIG` keys off the
  spawning *thread*, not the process), otherwise it falls back to the `atexit`
  path. Non-Linux behavior is unchanged.
- **Graph server can no longer hang on its own log output.** The child was
  spawned with `stdout`/`stderr=PIPE` that the parent drained only *after* the
  child exited; while it was alive the Werkzeug dev server's per-request stderr
  logging would fill the ~64KB kernel pipe buffer and block the child
  mid-session. Its output is now redirected to a per-port log file
  (`~/.agent-os/graph_server-<port>.log`) — file writes never block — and crash
  diagnostics are preserved by reading a bounded tail of that file post-exit
  instead of `communicate()`. If the log file can't be opened, it falls back to
  `DEVNULL` so the server still starts.

## [0.2.3] - 2026-06-21

### Added
- **Clickable task cards → card detail page in the graph UI.** Cards in the
  Flask task-cards list (`/<slug>/task_cards`) now link to a new
  `GET /<slug>/task_cards/<card_id>` detail page showing the card's full
  contents: title, status badge, priority, created/updated timestamps, the full
  description (whitespace preserved), and the `card_comments` work-log thread
  (author + timestamp + body, oldest→newest). Rows are clickable (title link +
  row affordance) and the detail page breadcrumbs back to the list. Empty
  description / zero comments render graceful placeholders; an unknown `card_id`
  or missing database returns a friendly 404, and a read error returns a generic
  500 with stderr-only logging.

### Security
- The detail route uses parameterized SQL (`WHERE card_id = ?`), `escape()`s
  every DB-sourced field (descriptions and comments are free-text XSS sinks), and
  `urllib.parse.quote(safe="")`s the slug and `card_id` in the `href`/`onclick`
  so neither can break out of the JS string or HTML attribute. It is a read-only
  `GET`, so the existing `X-Requested-With` CSRF guard is unaffected. The SQLite
  connection is closed on every path via `try/finally`.

## [0.2.2] - 2026-06-20

### Fixed
- **`db_get_table` / `db_get_column` now find objects that exist.** A second
  node-field mismatch (sibling of the 0.2.1 prefix fix): the tools gated on
  `node.get("type") == "Table"/"Column"`, but the build emits the type under
  **`node_type`** (there is no `type` key), so both returned "not found" for every
  real object. They now read `node_type`. `db_search_schema` likewise reads the
  real `node_type` (fixing its `object_type` filter) and surfaces
  `qualified_name` / `name` instead of the absent `label`. Found by querying the
  live DB graph and validated against the real `db_graph.json` node structure; the
  test fixtures were corrected to mirror the real node schema (the prior fixtures
  encoded the wrong fields — which is what hid both this and the prefix bug).

## [0.2.1] - 2026-06-20

### Performance
- **DB graph: stop rebuilding the whole schema on every `db_*` call.** Four changes:
  - **TTL cache** (`AGENT_OS_DB_GRAPH_TTL`, default 30s; `0` = always rebuild):
    consecutive `db_*` calls within the window reuse the last build instead of a fresh
    full pull; a failed rebuild serves the last-good graph with a warning.
  - **In-process build over a pooled pyodbc connection** replaces the two cold
    subprocess spawns + re-auth per call; the build core is now importable
    (`build_db_graph.build_graph_data` / `build_and_write`) with the CLI preserved.
  - **No HTML on the tool path** — `db_*` builds data only; the Flask UI still
    regenerates the pyvis HTML on its own refresh.
  - **Targeted bounded-neighborhood build** (`AGENT_OS_DB_GRAPH_DEPTH`): `db_get_table`,
    `db_get_column`, `db_get_table_relationships`, and `db_get_routine_dependencies`
    build only the subgraph around their entry object (table / column / function /
    procedure) to a bounded depth via WHERE-filtered BFS, instead of the full schema.
    `db_search_schema` and `db_find_relationship_path` keep the full (now cached,
    in-process) build.

### Security
- `refresh_database_graph` no longer returns raw exception text to callers — only
  controlled connection-setup messages; other errors are logged to stderr and
  generalized, avoiding internal-path disclosure (the same leak class fixed for the
  Flask UI in 0.1.14). Added a lock around the pooled connection's reopen sequence.

### Fixed
- **DB tools now actually return results.** The exact-match tools compared raw
  caller names against the build's *prefixed* node ids (`table:` / `column:` /
  `<routine>:`), so `db_get_table`, `db_get_column`, `db_get_table_relationships`,
  `db_get_routine_dependencies`, and `db_find_relationship_path` returned "not
  found" for every real object. They now normalize the prefix before matching
  (and `db_find_relationship_path` seeds its BFS with the prefixed table id, then
  strips prefixes in the returned path). Pre-existing bug, surfaced by the 0.2.1
  review.

### Notes
- The DB-graph changes are validated by mock-based tests only (no live SQL Server in
  CI); the `sys.*` WHERE-filtering should be smoke-tested against a real database.

## [0.2.0] - 2026-06-20

**Milestone release.** agent-os has grown from a task-cards MCP server into a full
agentic-development operating system: a delegation-first plugin where the main
Claude Code instance orchestrates a roster of **10 specialist agents** and **24
skills** — across implementation, investigation, review, security, testing,
verification, and data work — backed by repository and database knowledge graphs,
lifecycle hooks, and a per-repo graph UI. The 0.1.x series delivered the agent
roster, subagent nesting, the per-operation card database, the graph-UI security
hardening, and the graphify node-link traversal fix; 0.2.0 marks that maturity.

### Changed (documentation)
- **Release discipline: MCP-server code needs a per-session reconnect.**
  `mcp/DEPLOYMENT_CHECKLIST.md` now documents that changes to `mcp/*.py` take
  effect only after a `/mcp` reconnect (or a Claude Code restart) in each session
  — `/reload-plugins` reloads agents, skills, and hooks but cannot hot-reload the
  long-running MCP server's Python process. MCP servers are per-session, so other
  windows keep the old code until they reconnect; you don't need to close every
  window.

## [0.1.15] - 2026-06-20

### Fixed
- **Code-graph traversal was blind to real graphify output.** graphify emits
  NetworkX node-link JSON (top-level `links`, edges keyed `relation`, nodes keyed
  `file_type`), but the code-graph tools read `edges` / `relationship` / `type`,
  so `code_find_callers`, `code_impact_analysis`, `graph_get_neighbors`,
  `graph_get_subgraph`, and friends silently returned empty on every real graph —
  and the test fixture encoded the wrong shape, which hid it from CI.
  `graph_io.load_code_graph()` now normalizes graphify's format at load (additive,
  idempotent aliases: `links`→`edges`, `relation`→`relationship`,
  `file_type`→`type`), and `graph_status` counts links-or-edges. The database
  graph is unaffected (it already emits `edges`). The fixture was corrected to real
  graphify node-link format and a regression test added. Loading the live repo
  graph now exposes 896 edges (was 0).

## [0.1.14] - 2026-06-20

### Security
- **Graph UI: failed refreshes no longer leak subprocess output (or DB credentials).**
  `/<slug>/refresh/repo` and `/<slug>/refresh/db` previously returned the subprocess
  `stdout`/`stderr` in their 500 response; a failed DB refresh could echo
  `DB_CONNECTION_STRING` to the caller. Output is now logged server-side and the
  DB endpoint withholds it entirely (it may contain credentials).
- **Graph UI: CSRF guard on the refresh POST endpoints.** A `before_request` hook
  requires an `X-Requested-With: XMLHttpRequest` header on state-changing requests,
  so a forged cross-site POST can no longer trigger `graphify` or the DB build. The
  in-page refresh buttons send the header. (No authentication was added and the
  bind stays `0.0.0.0` — the deployment is an intentionally trusted LAN.)

  Both fixes came from an independent `security-reviewer` agent pass; XSS escaping,
  SQL, path-traversal, command-injection, and SSRF surfaces were reviewed and found
  clean. Regression tests added in `mcp/tests/test_flask_app.py`.

### Fixed (documentation)
- Root `CLAUDE.md` now states it documents the task-cards subsystem and points to
  `README.md` / `templates/CLAUDE.md` for the full plugin and operating model.
- `README.md` distinguishes `CLAUDE.md` (card tools) from `templates/CLAUDE.md`
  (operating model) and notes the `mcp/tests/` CI suite alongside `test_server.py`.
- Removed the unimplemented `config.toml` from the `INTEGRATION.md` layout diagram.

## [0.1.13] - 2026-06-19

### Added
- **Three specialist agents** that fill previously-idle capability gaps so the
  lead can delegate every domain:
  - **`frontend-engineer`** (sonnet) — owns UI work; wired to the
    `frontend-design` skill, the **playwright** browser MCP, the **LSP** servers,
    and **context7** for framework docs.
  - **`security-reviewer`** (sonnet, `effort: high`) — read-only review of
    security-sensitive diffs via the **security-guidance** plugin and the
    `/security-review` skill.
  - **`verification-engineer`** (sonnet) — runs the real app / browser
    (`verify`, `run`, playwright) to confirm behavior, distinct from
    `test-engineer`'s suites.
- **Four cross-cutting skills**: `lsp-diagnostics` (pull LSP diagnostics after
  edits), `library-docs` (fetch current docs via context7 before coding against
  an unfamiliar dependency), `browser-verification` (drive the UI via
  playwright), and `runtime-verification` (run the real app via verify/run).

### Changed
- The implementers and testers now reach idle tooling: `implementer`,
  `complex-implementer`, `test-engineer`, and `database-engineer` gained the
  `LSP` tool and load `lsp-diagnostics`; the two implementers also gained
  **context7** and load `library-docs`. Agent count 7→10, skill count 20→24;
  delegation rules in `templates/CLAUDE.md` updated.

## [0.1.12] - 2026-06-19

### Added
- **`complex-implementer` agent** (`model: opus`, `effort: xhigh`) for difficult,
  architecturally significant, or repo-wide changes — large refactors, new
  subsystems, edits to shared/central code. It maps blast radius with the code
  graph (`code_impact_analysis` / `code_find_callers` / `graph_get_subgraph`)
  before editing and plans coherent multi-file changes. `implementer` (sonnet)
  remains the default for simple, well-scoped work. Routed via the delegation
  rules in `templates/CLAUDE.md` and the `execute-card` skill.
- **`codebase-consultant` agent** (`model: sonnet`, `effort: medium`) — a
  read-only repository investigator. Delegate "how does X work / where is Y /
  what depends on Z / is this doc current" questions to it and the heavy
  searching and file reading happen in ITS context, returning a tight, sourced
  answer (`file:line` + graph node IDs) instead of bloating the caller's
  context. Ships three skills: `graph-file-discovery` (graph-first),
  `search-to-graph` (grep/tree → graph), and `doc-review` (audit docs against
  the code). The five doing/reviewing agents (`implementer`,
  `complex-implementer`, `code-reviewer`, `test-engineer`, `database-engineer`)
  gained the `Agent` tool so they can delegate to it as a nested subagent
  (Claude Code supports subagent nesting to depth 5).

### Changed
- Skills `card-workflow`, `graph-query-discipline`, and `sql-routine-analysis`
  now explicitly point to the repository **code graph** for scoping and impact
  (the dedicated database-graph skills were intentionally left focused on the DB
  graph). The five existing agents already referenced the code graph.

## [0.1.11] - 2026-06-19

### Fixed
- **Card writes no longer flip to read-only after `cards.sqlite` is replaced.**
  The server previously opened one SQLite connection at startup and held it for
  the whole process; when the database file's inode was swapped underneath it (a
  git operation touching `.agent-os/`, an external rewrite, or delete+recreate),
  SQLite returned `SQLITE_READONLY_DBMOVED` and every subsequent card write
  failed read-only. `card_tools` now opens a fresh short-lived connection per
  operation from a fixed path — which always resolves to the current file — and
  re-ensures the schema on each connect, so a replaced or empty file self-heals.
  `server.py` no longer keeps a long-lived card connection (`shutdown_db` only
  stops the graph server now). Storage location is unchanged
  (`<repo>/.agent-os/cards.sqlite`); no migration needed. Regression test:
  `mcp/tests/test_card_db_resilience.py`.

## [0.1.10] - 2026-06-19

### Added
- **Test suite + CI:** new `mcp/tests/` covering the code-graph tools, shared
  graph tools, the Flask navigation routes (incl. XSS-escaping assertions), and
  `scripts/hook_common.py`, plus `mcp/test_probe_health.py`. A GitHub Actions
  workflow (`.github/workflows/ci.yml`) runs `py_compile` + the full suite on
  push/PR. db-graph tools are skipped in CI (need pyodbc + live SQL Server).
  Dev-only test deps live in `dev-requirements.txt`.
- **Fresh-install smoke test** (`mcp/smoke_test.py`): provisions a throwaway
  venv, boots the MCP server over stdio (asserting a clean newline-delimited
  JSON-RPC handshake with no stdout corruption), checks Flask `/health`, and
  round-trips a card. Documented in `mcp/DEPLOYMENT_CHECKLIST.md`.

### Changed
- **`server.py` split:** the 1,435-line monolith is now a thin entrypoint that
  registers all 24 tools from focused modules — `card_tools`, `code_graph_tools`,
  `shared_graph_tools`, `db_graph_tools`, `graph_io`, `graph_server`. No tool
  names, signatures, or behavior changed. Tool modules reference the graph
  loaders / `get_repo_root` via the `graph_io.` namespace so those seams are
  patchable in tests.
- **Documentation consolidation:** removed the stale `PROJECT_SUMMARY.md` and
  `NEW_MCP_TOOLING.md`; corrected drift across `README.md` / `CLAUDE.md` /
  `mcp/DEPLOYMENT_CHECKLIST.md` / `hooks/README.md` — per-repo graph-UI URL
  (`/<slug>/`), `graphify update . --force`, `flask` + `python-dotenv` as core
  deps, and tool counts.

### Fixed
- **Graph-server lifecycle robustness:** `start_graph_server()` now probes
  `/health` before reusing an in-use port (a foreign or dead process holding the
  port is detected and logged instead of assumed healthy), surfaces a crashed
  child's stderr, and respawns a dead child via `_check_graph_server_health()`.
  Stdlib only (`http.client`); no new dependency.

## [0.1.9] - 2026-06-19

### Changed
- **Graph-server navigation pages restyled (dark mode):** the home page,
  per-repo page, task-cards view, and error pages in `mcp/db_tools/app.py` now
  share one dark theme. Extracted the three duplicated light-mode `<style>`
  blocks into a single `_PAGE_CSS` constant and a `_page()` wrapper helper;
  repos render as cards on the home page, task statuses as colored pill badges.
  The graphify/db graph pages themselves are generated externally and are
  unchanged. XSS escaping (`escape` for HTML text, `urllib.parse.quote` for
  URL/JS contexts) is preserved throughout; `_missing_file` now URL-encodes the
  slug in its back-link.

## [0.1.8] - 2026-06-19

### Fixed
- **Graph refresh errored on code-deleting refactors:** all three graphify
  refresh sites — `graphify_command()` in `scripts/hook_common.py`, the
  `graph_refresh` MCP tool in `mcp/server.py`, and the `/<slug>/refresh/repo`
  route in `mcp/db_tools/app.py` — now run `graphify update . --force`. Without
  `--force`, graphify refuses to overwrite `graph.json` when a rebuild produces
  fewer nodes (e.g. after a refactor that deletes code), so the refresh reported
  an error and the graph went stale. `--force` overwrites in that case, keeping
  incremental refresh reliable. The `AGENT_OS_GRAPHIFY_ARGS` escape hatch still
  appends after the flag.

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
