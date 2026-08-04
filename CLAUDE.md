# Task Cards MCP Server

Jira-style task management system for Claude Code and AI agents. Repository-local, SQLite-backed, built on the `mcp` Python package.

> **Scope of this document:** this file documents the **task-cards MCP subsystem** (the 6 card tools, the SQLite schema, and the basic card workflow), plus the **optional central memory subsystem** (the 3 `memory_*` tools) covered in its own section near the end. It is one part of the larger **agent-os** plugin, which also ships the code/database knowledge-graph tools, lifecycle hooks, and a delegation-oriented set of agents and skills. For the full plugin overview and install, see [`README.md`](README.md); for the agent operating model and delegation rules, see [`templates/CLAUDE.md`](templates/CLAUDE.md).

## What This Is

A lightweight task/card management system designed as an MCP server. It provides:

- **Repository-local storage**: All task data lives in `.agent-os/cards.sqlite` within your repo
- **Simple card lifecycle**: Created → In Progress → Complete
- **Work logs**: Comment system for tracking progress
- **Flexible filtering**: Query cards by status or priority
- **Card subsystem has zero external dependencies**: pure SQLite, no external services. The optional database-graph subsystem has extra dependencies (`pyodbc` plus a live SQL Server) and is not required to use cards.

## Installation

Run the bootstrap installer for your platform. It creates a plugin-local `.venv`,
installs the core dependencies, **generates** `.mcp.json` and `hooks/hooks.json`
from the committed templates in `templates/` (substituting the venv interpreter's
absolute path for `@@PYTHON@@`), and verifies the result with a real MCP handshake.

```bash
# POSIX (Linux/macOS):
installer/install.sh              # add --with-db / --with-memory / --all-extras
# Windows (PowerShell 5.1+):
# powershell -ExecutionPolicy Bypass -File installer\install.ps1
```

The generated `.mcp.json` / `hooks/hooks.json` are git-ignored (only templates are
committed); on a fresh clone they are absent until the installer runs, which is
safe — Claude Code simply registers no server and fires no hooks until then. See
`README.md` and `WINDOWS.md` §5 for the full rationale. The core dependency set
(`mcp`, `flask`, `python-dotenv`) can still be installed directly with
`pip install -r mcp/requirements.txt` if you are wiring the server by hand.

## Usage

The server exposes the following tools via MCP:

### create_card
Create a new task card.

**Inputs:**
- `title` (string, required): Card title
- `description` (string, optional): Detailed description
- `priority` (string, optional, default="medium"): Priority level

**Returns:** Card object with `card_id`, status "Created"

```python
create_card(
    title="Implement user auth",
    description="Add JWT-based authentication",
    priority="high"
)
```

### list_cards
List cards with optional filtering.

**Inputs:**
- `status` (string, optional): Filter by "Created", "In Progress", or "Complete"
- `priority` (string, optional): Filter by priority

**Returns:** List of card objects

```python
list_cards(status="In Progress")
list_cards(priority="high")
```

### get_card
Retrieve a single card with all comments.

**Inputs:**
- `card_id` (string, required): The card's unique ID

**Returns:** Card object with `comments` array

### update_card
Update card fields.

**Inputs:**
- `card_id` (string, required)
- `title`, `description`, `priority`, `status` (all optional)

**Returns:** Updated card object

**Valid status values:** "Created", "In Progress", "Complete"

### add_comment
Add a work log entry to a card.

**Inputs:**
- `card_id` (string, required)
- `author` (string, required): Who made the comment
- `comment` (string, required): The comment/work log text

**Returns:** Comment object

### complete_card
Mark a card as Complete with a summary.

**Inputs:**
- `card_id` (string, required)
- `completion_summary` (string, required): Final summary of work

**Returns:** Updated card with status "Complete"

## Database Schema

Cards are stored in `.agent-os/cards.sqlite`:

```sql
CREATE TABLE cards (
    card_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL,
    priority TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE TABLE card_comments (
    comment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT NOT NULL,
    author TEXT,
    comment TEXT,
    created_at TIMESTAMP
);
```

## Recommended Workflow

### For Users

1. Create a card for significant work
2. Move it to "In Progress" when starting
3. Add comments to track progress
4. Complete the card when done

### For Claude Code Integration

Enable automatic card management in `CLAUDE.md` / project settings:

- ✅ Always create a card for multi-step tasks
- ✅ Log progress with `add_comment`
- ✅ Update status before switching tasks
- ✅ Complete cards after verification

## Example Workflow

```
User: "Implement user authentication"
   ↓
Claude creates card:
   id: ab12cd34
   title: Implement user authentication
   status: Created
   ↓
Claude updates: status → "In Progress"
   ↓
Claude works, adds comments:
   "Started implementing JWT strategy"
   "Database schema updated"
   "Testing basic flow"
   ↓
Claude completes card:
   completion_summary: "JWT auth fully implemented and tested"
   status: Complete
```

## Central Memory Subsystem (optional)

Beyond cards, the server ships an **optional** central memory store (Phase 0):
ingest → chunk → embed → retrieve over a machine-global corpus, exposed via three
MCP tools. It is optional in exactly the way the SQL-Server database-graph
subsystem is: importing it never pulls its heavy dependencies, the server starts
and all card/graph tools keep working without them, and the `memory_*` tools
return a clear "unavailable — install `mcp/memory_requirements.txt`" result
instead of raising when the extras are absent.

**Install extras:** `pip install -r mcp/memory_requirements.txt`
(`sqlite-vec`, `pysqlite3-binary`, `sentence-transformers`). The embedding model
`BAAI/bge-small-en-v1.5` (384-dim) downloads once and is then fully offline.
`pysqlite3-binary` is required because many Python builds compile the stdlib
`sqlite3` without loadable-extension support, which `sqlite-vec` needs.

**Tools:**

- `memory_ingest(path, source_type=None, published_at=None)` — ingest a file or
  directory; auto-detects a ChatGPT `conversations.json` export vs. Markdown/text.
  `source_type='news'` requires `published_at`. Content-hash dedup makes an
  unchanged re-ingest a no-op and cleanly replaces a changed file's chunks.
- `memory_query(query, top_k=8, source_type=None, date_from=None, date_to=None)`
  — hybrid BM25 (FTS5) + vector kNN (sqlite-vec) fused with RRF (k=60), with
  optional source-type and date-range filters.
- `memory_status()` — availability, corpus stats by source type, embedding model,
  and DB path/size.

**Storage:** `~/.agent-os/central-memory/memory.sqlite` — machine-global, **not**
repo-local (the corpus is inherently cross-repo). Tables (schema `user_version` 1):
`documents`, `chunks` (`id INTEGER PRIMARY KEY` rowid alias, chunk ids
`mem:<doc_id>:<seq>` as a `UNIQUE` key, embedding model + dim recorded per row), a
`chunks_fts` FTS5 **external-content** index kept in lockstep by triggers, and a
`chunks_vec` vec0 table keyed to `chunks.id`. `source_type` is validated in app
code (no DB CHECK). Set `AGENT_OS_MEMORY_HOME` to relocate the store (the tests
use this so they never touch your real home). Runs in WAL mode with the same
per-operation connection discipline as the card DB.

Out of scope for Phase 0 (later phases): graph edges, agent-writable nodes,
reranker, decay ranking, consolidation, PDF ingestion, and automated news
fetching.

## Extending

Future features (not in scope yet):
- Agent ownership / assignment
- Card dependencies
- Card-to-file links
- Memory integration
- Multi-agent coordination
- Priority queues

## Files

- `mcp/server.py` - Thin MCP entrypoint; tools live in `mcp/*_tools.py` modules
- `mcp/requirements.txt` - Core dependencies (`mcp`, `flask`, `python-dotenv`)
- `mcp/db_requirements.txt` - Optional database-graph extras (`pyodbc`, `networkx`, `pyvis`)
- `mcp/memory_tools.py` + `mcp/memory/` - Optional central memory subsystem (see above)
- `mcp/memory_requirements.txt` - Optional memory extras (`sqlite-vec`, `pysqlite3-binary`, `sentence-transformers`)
- `templates/mcp.json.tmpl`, `templates/hooks.json.tmpl` - Committed launcher-config templates (interpreter = `@@PYTHON@@`)
- `scripts/generate_config.py` - Generates `.mcp.json` + `hooks/hooks.json` from the templates
- `installer/install.sh`, `installer/install.ps1` - Per-platform bootstrap installers (venv + deps + generate + verify)
- `installer/verify_install.py` - MCP handshake verifier (`initialize` + `tools/list`, asserts 27 tools)
- `CLAUDE.md` - This documentation
- `.agent-os/cards.sqlite` - Repository-local database (created on first run)
- `.agent-os/sync/` - Local event outbox + drain state: `outbox.jsonl` (append-only `EventEnvelope`-per-line, written by card/graph/hook ops), `cursor.json` (drain position), `dead-letter.jsonl`, `outbox.log`, and `drain.pid` (the sync companion's single-instance guard). Produced by the top-level `outbox/` package; gitignored like the rest of `.agent-os/`. **Phase 1c added the network drain** (`outbox/drain.py`) that ships these events to the central `/v1` API — but it is **opt-in and OFF by default**: nothing phones home unless `AGENT_OS_SYNC=1` opts the MCP server into spawning the drain companion (see the Notes below and `services/README.md`).

## Notes

- Cards are repository-local; each repo has its own database
- The MCP server auto-initializes the database on first connection
- The card subsystem requires no external services; the optional database-graph subsystem does (`pyodbc` + a live SQL Server)
- **Event emission (Phase 1b):** card create/update/complete, graph refreshes, and the session/subagent lifecycle hooks append a small validated `EventEnvelope` to `.agent-os/sync/outbox.jsonl`. It is best-effort and can never fail a card/graph/hook op. Kill-switch: set `AGENT_OS_EVENTS_DISABLED=1` to disable all emission. Durability: appends are not `fsync`ed per event by default (hot-path buffer); set `AGENT_OS_EVENTS_FSYNC=1` to force an `fsync` after every append.
- **Network sync (Phase 1c), opt-in and OFF by default:** a drain worker (`outbox/drain.py`) ships outbox events to the central `/v1` API (`services/api/`) with a safe byte-offset cursor (a network outage costs a retry, never a lost or double-recorded event — ingest is idempotent on `event_id`). The MCP server spawns it as a background companion **only** when `AGENT_OS_SYNC=1` is set (mirrors the graph-server spawn: pdeathsig, respawn-on-crash, per-repo append log under `~/.agent-os/sync_worker-<repo>.log`, single-instance `.agent-os/sync/drain.pid` guard). With `AGENT_OS_SYNC` unset, no network process is ever created. Drain config: `AGENT_OS_API_URL` (default `http://127.0.0.1:8765`), `AGENT_OS_API_KEY`, `AGENT_OS_SYNC_INTERVAL`/`_BATCH`/`_TIMEOUT`, and the `AGENT_OS_SYNC_DISABLED=1` drain kill-switch. See `services/README.md`.
- `.agent-os/` is auto-ignored by git: the plugin drops a self-protecting `.agent-os/.gitignore` (wildcard `*`) on first init, so `cards.sqlite` is never tracked. Do NOT commit the live SQLite file — committing it risks silently losing cards when `git reset --hard`, checkout, or rebase reverts the DB to an older snapshot. To share cards with teammates, use export rather than committing the live database. Note that `git clean -fdx` (the `-x` flag deletes ignored files) will still remove the ignored `.agent-os/` directory and its card DB — use `git clean -fd` without `-x`, or `-e .agent-os`, to preserve it.
