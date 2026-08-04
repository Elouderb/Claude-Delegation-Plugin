# Agent OS

A repository-local "operating system" for agentic development in Claude Code, packaged as a Claude Code plugin (version `0.2.7`). It bundles task tracking, code/database knowledge graphs, lifecycle hooks, and a delegation-oriented set of agents and skills.

## What's in the plugin

| Component | Location | Purpose |
|-----------|----------|---------|
| **Task cards MCP server** | `mcp/server.py` | Jira-style cards in a repo-local SQLite DB (`.agent-os/cards.sqlite`). 6 card tools. |
| **Graph query tools** | `mcp/server.py` | 18 read-oriented MCP tools over the Graphify code graph and the database graph. |
| **Central memory tools** | `mcp/memory_tools.py` | 3 optional tools (`memory_ingest`, `memory_query`, `memory_status`) over a machine-global sqlite-vec + FTS5 corpus. Degrades gracefully when its extras are absent (see below). |
| **Database graph builder** | `mcp/db_tools/` | Builds a graph of a Microsoft SQL Server schema (`build_db_graph.py` + `build_graph_html.py`). Optional; requires a SQL Server connection. |
| **Architecture contracts** | `contracts/` | Phase 0 typed contract models (pydantic v2) for events, agents, tasks/sync, memory, capabilities, and context, plus stable machine/repo identity and committed JSON Schemas (`contracts/schemas/*.json`). See [`docs/architecture/CONTRACTS.md`](docs/architecture/CONTRACTS.md). |
| **Local sync outbox** | `outbox/` | Phase 1b offline event plane: identity bootstrap plus a durable append-only `.agent-os/sync/outbox.jsonl` of validated `EventEnvelope`s emitted at the plugin's existing lifecycle points (card create/update/complete, graph refresh, session/subagent hooks). Draining to the central store is done by the Phase 1c drain worker (`outbox/drain.py`); `AGENT_OS_EVENTS_DISABLED=1` disables emission entirely. |
| **Central store migrations** | `database/` | Phase 1a versioned SQL migrations + a stdlib/pyodbc runner that stand up the `registry.*` / `ops.*` schema in the central SQL Server database (`agent_os_memory`). Optional; requires `pyodbc` + a connection string. See [`database/README.md`](database/README.md). |
| **Central Agent OS API + drain** | `services/api/`, `outbox/drain.py` | Phase 1c `/v1` HTTP API in front of the central store (machine/repo/session registration, heartbeats, idempotent event ingestion, read endpoints, `GET/PUT /v1/sync/{client_id}` replay cursors) plus the drain worker that ships outbox events to it. Bearer-auth (`AGENT_OS_API_KEY`); `python -m services.api` / `python -m outbox.drain [--once]`. The drain can also run as a server-spawned companion, OFF by default (opt in with `AGENT_OS_SYNC=1`). See [`services/README.md`](services/README.md). |
| **M3 stand-in client** | `services/m3_client/` | Phase 1d read-only terminal client playing Machine 3's role until the real M3 VM exists: `python -m services.m3_client status [--json] [--follow]` renders machines/repositories/active sessions/recent events from the `/v1` API, with a gap-free `--follow` keyset walk and `--client-id`-scoped resume position. Stdlib-only end to end (no `contracts`/`pydantic`/`python-dotenv`) so it can eventually run standalone on a bare-Python M3 VM. See [`services/README.md`](services/README.md). |
| **Graph-sync hooks** | `hooks/hooks.json`, `scripts/` | Keep the repository graph fresh, protect generated files, and (Phase 1b) emit agent lifecycle events at real hook points (session-start / subagent-stop / session-end). See `hooks/README.md`. |
| **Agents** | `agents/` | `implementer`, `complex-implementer` (opus / high-effort, for repo-wide or complex changes), `frontend-engineer` (UI), `codebase-consultant` (read-only repo investigator other agents can delegate to), `code-reviewer`, `security-reviewer`, `test-engineer`, `verification-engineer` (runs the real app / browser), `database-engineer`, `research-planner`. |
| **Skills** | `skills/` | 24 workflow skills for cards, planning, review, testing, codebase investigation, verification, and graph discipline. |

The MCP server is named `task-cards` and exposes 27 tools total: 6 card + 18 graph/code/db + 3 optional central-memory tools (the memory tools report themselves unavailable, rather than failing the server, when their extras are not installed).

## Installation

This is a Claude Code plugin. **Run the bootstrap installer first** — it creates a
plugin-local `.venv`, installs dependencies, and *generates* the launcher config
(`.mcp.json` and `hooks/hooks.json`) with the correct interpreter path. Then
register the bundled local marketplace so the manifest
(`.claude-plugin/plugin.json`), MCP server, hooks, agents, and skills are all
discovered automatically.

```bash
# 1. Bootstrap: create .venv, install deps, generate config, verify the handshake.
#    POSIX (Linux/macOS):
installer/install.sh
#    Windows (PowerShell 5.1+):
#    powershell -ExecutionPolicy Bypass -File installer\install.ps1

# 2. Register the local marketplace defined in .claude-plugin/marketplace.json
#    (marketplace name: agent-os-local) and install the plugin:
/plugin marketplace add /path/to/agent-os
/plugin install agent-os@agent-os-local

# 3. Then /reload-plugins (or restart Claude Code). Verify the server with /mcp.
```

Both installers accept the same optional extras (prompted interactively, or
selected non-interactively for CI):

| Selection | Flag (both) | Environment |
|-----------|-------------|-------------|
| Database-graph extra (`pyodbc`, `networkx`, `pyvis`) | `--with-db` / `-WithDb` | `AGENT_OS_INSTALL_DB=1` |
| Central-memory extra (`sqlite-vec`, …) | `--with-memory` / `-WithMemory` | `AGENT_OS_INSTALL_MEMORY=1` |
| Both | `--all-extras` / `-AllExtras` | — |
| Neither (force both off, no prompt) | `--no-extras` / `-NoExtras` | — |
| Never prompt | `-y` / `--non-interactive` / `-NonInteractive` | `AGENT_OS_NONINTERACTIVE=1` (implied by `CI`) |
| Interpreter for the venv | — (env only) | `AGENT_OS_PYTHON` (default `python3`; on Windows a `py -3.12`/`py -3` launcher probe) |

The final step of each installer is a real **MCP handshake**
(`installer/verify_install.py`): it spawns the generated server command, performs
`initialize` + `tools/list` over stdio, and asserts the full registered tool set
is present — proving the interpreter, dependencies, and config agree.

### Generated launcher config (why there is no checked-in interpreter path)

`.mcp.json` and `hooks/hooks.json` are **generated artifacts** (git-ignored),
produced by `scripts/generate_config.py` from the committed templates in
`templates/`. Only the interpreter is substituted (for `@@PYTHON@@`); everything
else — the `${CLAUDE_PLUGIN_ROOT}` paths, the eight hook events — is preserved
verbatim. This removes the one non-portable, OS-specific path from version control
(see [`WINDOWS.md`](WINDOWS.md) §5): on Windows a checked-in `python3` resolves to
a Microsoft Store stub and silently no-ops the server and every hook (§1).

**Fresh clone (before running the installer):** the generated files do not exist
yet. That is safe — Claude Code discovers `.mcp.json` and `hooks/hooks.json` by
default location, and when they are absent there is simply nothing to register:
the MCP server is not loaded and the lifecycle hooks never fire (they are only
ever invoked *through* `hooks/hooks.json`). Nothing crashes. Run the installer to
generate them; re-running it any time is safe and idempotent.

> The interpreter discovery differs per platform on purpose: `install.ps1` uses
> the `py` launcher (`py -3.12`, then `py -3`) and **never** probes `python3` (the
> Store-stub trap); `install.sh` uses `python3`, which is correct on POSIX.

> **For local development of the plugin**, you can live-load the directory instead:
> `claude --plugin-dir /path/to/agent-os`. This is a dev convenience, not the
> primary install path.

`graphify` must be on your `PATH` for the code/database graph features and the
graph-sync hooks (which run `graphify update .`) to work.

### Upgrading an existing install (from ≤ 0.2.7)

`.mcp.json` and `hooks/hooks.json` became **generated, git-ignored artifacts** in
this release (they were previously committed with a hard-coded `python3`). After
you `git pull` a version at or after this change, an existing checkout no longer
tracks them, so:

- **Re-run the installer** (`installer/install.sh`, or `installer/install.ps1` on
  Windows) to regenerate both files for your machine. Until you do, the plugin
  registers **nothing** — the MCP server is not loaded and the lifecycle hooks
  never fire. That is silent by design (same as the fresh-clone note above), not
  an error; it just means the plugin is not yet installed on this checkout.
- If you had **hand-edited** `.mcp.json` / `hooks/hooks.json` locally (for example
  to work around the Windows `python3` bug), `git pull` will refuse to overwrite
  them. **Stash or discard** those local copies first (`git checkout -- .mcp.json
  hooks/hooks.json`, or `git stash`), then re-run the installer — these are now
  generated from `templates/`, so any change belongs in the template or the
  generator, not the output. This mirrors the one-time `git rm -r --cached
  .agent-os/` migration introduced in 0.2.7.

### Graph UI

The graph tooling serves a Flask web UI, namespaced per repository. Open
<http://localhost:5000/> for the list of active repos, then
`http://localhost:5000/<repo-slug>/` for that repo's code graph, database graph,
and task cards. The port is configurable via the `AGENT_OS_GRAPH_PORT`
environment variable. The MCP server reuses an already-running graph server on
that port instead of spawning a duplicate, so the main loop and subagents don't
collide. `flask` and `python-dotenv` are **core** requirements (in
`mcp/requirements.txt`), since this UI serves the code graph and cards — not just
the optional database subsystem.

### Artifact locations

| Artifact | Location |
|----------|----------|
| Task cards | `.agent-os/cards.sqlite` (repo-local) |
| Database graph | `.agent-os/db/` |
| Code graph | `graphify-out/` |
| Central memory | `~/.agent-os/central-memory/memory.sqlite` (machine-global) |

### Optional: database graph

The `db_*` graph tools and `mcp/db_tools/` connect to a Microsoft SQL Server instance. This subsystem has optional extra dependencies — `pyodbc`, a live SQL Server, and `DB_CONNECTION_STRING` set in `.env`. Copy `.env.example` to `.env` and set `DB_CONNECTION_STRING`. The `.env` file is gitignored and must never be committed. If you don't use the database tools, you can ignore this entirely.

### Optional: central memory (Phase 0)

A fully-local, machine-global corpus store: ingest documents, ChatGPT
conversation exports, and dated news; chunk and embed them; retrieve with hybrid
BM25 + vector search — all offline once the embedding model is cached. Like the
database graph, this subsystem is **optional**: the server starts and every
other tool works without it; the `memory_*` tools report themselves unavailable
until the extras are installed.

```bash
pip install -r mcp/memory_requirements.txt   # sqlite-vec, pysqlite3-binary, sentence-transformers
```

`pysqlite3-binary` is included because many Python builds ship a stdlib `sqlite3`
compiled without loadable-extension support (so `sqlite-vec` cannot load through
it); it bundles a modern SQLite with extensions enabled. The embedding model
(`bge-small-en-v1.5`, 384-dim) downloads once on first use, then runs offline.

**Memory tools** (registered on the same server):

| Tool | Purpose |
|------|---------|
| `memory_ingest(path, source_type=None, published_at=None)` | Ingest a file or directory. Auto-detects a ChatGPT `conversations.json` export vs. Markdown/text. `source_type='news'` requires `published_at`. Content-hash dedup: re-ingesting an unchanged file is a no-op; a changed file replaces its chunks. |
| `memory_query(query, top_k=8, source_type=None, date_from=None, date_to=None)` | Hybrid BM25 (FTS5) + vector kNN (sqlite-vec) fused with RRF (k=60). Optional `source_type` / date-range filters (matched against `published_at`, falling back to `ingested_at`). |
| `memory_status()` | Availability, corpus stats by source type, embedding model, and DB path/size. |

All memory state lives under `~/.agent-os/central-memory/` (machine-global, **not**
repo-local — the corpus is inherently cross-repo). Set `AGENT_OS_MEMORY_HOME` to
relocate it (used by the test suite so it never touches your real home).

## Card lifecycle

```
Created → In Progress → Complete
```

**Card tools:** `create_card`, `list_cards`, `get_card`, `update_card`, `add_comment`, `complete_card`.

```python
create_card(title="Implement OAuth2 flow", priority="high")
update_card(card_id, status="In Progress")
add_comment(card_id, author="claude", comment="JWT implementation complete")
complete_card(card_id, completion_summary="OAuth2 integrated and tested")
```

Cards are repository-local: each repo gets its own `.agent-os/cards.sqlite`, discovered via the `.git` root.

## Documentation

- `CLAUDE.md` — task-card tool reference (the 6 card tools, the SQLite schema, and the basic card workflow).
- `templates/CLAUDE.md` — the agent operating model and delegation rules (the template installed into consuming repos).
- `INTEGRATION.md` — project setup and workflow rules.
- `hooks/README.md` — how the graph-sync hooks behave.
- `CHANGELOG.md` — change history.

### Architecture / where the plan lives

The long-term buildout of Agent OS into a distributed control/execution system
is described in [`docs/architecture/PROPOSAL.md`](docs/architecture/PROPOSAL.md)
(moved from the repo root). Phase 0 ships stable, typed **contracts** — pydantic
v2 models plus committed JSON Schemas — in the top-level `contracts/` package:

- [`docs/architecture/CONTRACTS.md`](docs/architecture/CONTRACTS.md) — contract
  models, prefixed-ULID identifiers, identity files, capability/versioning rules.
- [`docs/architecture/EVENTS.md`](docs/architecture/EVENTS.md) — the event
  envelope and taxonomy, and the CloudEvents mapping decision.

Run `python -m contracts.examples` to print one valid example payload per model,
or `python -m contracts.generate_schemas --check` to verify the committed schemas.

## Testing

```bash
cd mcp && python3 test_server.py   # the 6 card tools (8/8)
python3 -m pytest mcp/tests/       # full CI suite: code graph, Flask routes, hooks, DB resilience
```

See `mcp/example_usage.py` for runnable usage patterns.

## Status

The card system is functional and tested. The database-graph subsystem is optional and requires `pyodbc`, a live SQL Server, and a `DB_CONNECTION_STRING` in `.env`. See `CHANGELOG.md` for the change history.
