# Task Cards — Subsystem Summary

> **Scope note:** This document summarizes the **task-cards subsystem** only. It is
> one part of the larger **Agent OS** plugin (version 0.1.1), which also includes
> 18 code/database graph tools, graph-sync hooks, 5 agents, and 17 skills. See
> `README.md` for the full plugin overview and install instructions, and
> `CHANGELOG.md` for version history.

## Status: card system functional and tested

A repository-local task management system for Claude Code and AI agents.

---

## What Was Built

### Core System
- **MCP Server** (`mcp/server.py`): MCP-based server hosting 6 task management
  tools alongside the 18 code/database graph tools (24 tools total).
- **SQLite Database**: Repository-local storage in `.agent-os/cards.sqlite`.
- **Card Lifecycle**: Created → In Progress → Complete.
- **Work Logs**: Comment system for tracking progress and decisions.

The single `mcp` package is the only required dependency (see
`mcp/requirements.txt`). The database-graph subsystem has optional dependencies
(`pyodbc`, a live SQL Server, and a `.env` `DB_CONNECTION_STRING`).

---

## Features Implemented

### Card MCP Tools (6 total)

1. **create_card** - Create new task with title, description, priority
2. **list_cards** - Query cards with optional status/priority filters
3. **get_card** - Retrieve full card with all comments
4. **update_card** - Modify title, description, priority, status
5. **add_comment** - Log work progress and decisions
6. **complete_card** - Mark task as Complete with summary

### Database Schema

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

---

## Key Design Decisions

### Repository-Local Storage
- Each repo has its own `.agent-os/cards.sqlite`
- No external services or databases
- Auto-discovers git root for proper isolation
- Safe to commit to version control

### Simple Card States
- Created: Newly created task
- In Progress: Currently being worked on
- Complete: Finished with summary

### Work Log Tracking
- Comments tied to cards
- Author and timestamp for each entry
- Retrieved with full card data
- Useful for progress tracking and handoff

---

## Testing

The card tools are covered by `mcp/test_server.py` (8/8 passing):

```
✓ Database initialization
✓ Card creation
✓ List with filters
✓ Card retrieval with comments
✓ Card updates (status, priority, etc.)
✓ Comment creation
✓ Card completion
✓ Status filtering
```

Run tests: `python3 mcp/test_server.py`

---

## Installation

See `README.md` for the canonical install flow. In brief:

```bash
pip install -r mcp/requirements.txt
# then, in Claude Code:
/plugin marketplace add /path/to/agent-os
/plugin install agent-os@agent-os-local
```

(`claude --plugin-dir /path/to/agent-os` is a dev-only alternative.)

---

## Architecture Notes

### Database Discovery
- Finds repo root by looking for `.git`
- Creates `.agent-os/` if needed
- Initializes SQLite on first connection
- Uses `sqlite3.Row` factory

### Tool Implementation
- Dynamic SQL queries for flexible filtering
- UUID-based card IDs (8-char shortened)
- ISO timestamp tracking (created_at, updated_at)
- Status validation (only the 3 valid states accepted)

### Error Handling
- Returns meaningful error messages
- Validates card existence before operations
- Graceful handling of missing fields in updates

---

## What's NOT Included (Intentional Scope)

Still out of scope for the **card subsystem** specifically:

- Agent ownership/assignment
- Card dependencies
- Card-to-file links
- Priority queues
- Archive functionality
- Custom fields

The card subsystem stays intentionally simple. Note that at the **plugin** level,
some of the originally "future" items now exist outside the card schema:
code/database **graph integration** (the 18 `code_*` / `db_*` / `graph_*` tools)
and **multi-agent coordination** (the `agents/` and `skills/` directories).
