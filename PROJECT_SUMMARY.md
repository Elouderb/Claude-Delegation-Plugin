# Task Cards — Subsystem Summary

> **Scope note:** This document summarizes the **task-cards subsystem** only. It is
> one part of the larger **Agent OS** plugin, which also includes 18 code/database
> graph tools, graph-sync hooks, 5 agents, and 17 skills. See `README.md` for the
> full plugin overview and `mcp/DEPLOYMENT_CHECKLIST.md` for the current file
> layout. Some details below (line counts, the illustrative file tree) describe an
> earlier standalone layout and have since evolved — `server.py` now lives in
> `mcp/` and also hosts the graph tools.

## Status: card system functional and tested

A repository-local task management system for Claude Code and AI agents.

---

## What Was Built

### Core System
- **MCP Server** (`server.py`): FastMCP-based server with 6 task management tools
- **SQLite Database**: Repository-local storage in `.agent-os/cards.sqlite`
- **Card Lifecycle**: Created → In Progress → Complete
- **Work Logs**: Comment system for tracking progress and decisions

### Files Delivered

```
task-cards/
├── server.py                    (Main MCP server - 215 lines)
├── test_server.py              (Test suite - 130 lines, all passing)
├── requirements.txt            (Dependencies: mcp, fastmcp)
├── install.sh                  (Setup script)
├── README.md                   (Quick start guide)
├── CLAUDE.md                   (Comprehensive documentation)
├── INTEGRATION.md              (Setup & integration guide)
├── example_usage.py            (Usage examples & patterns)
├── mcp_settings_example.json  (Configuration template)
└── .gitignore                  (Python/IDE ignores)
```

---

## Features Implemented

### MCP Tools (6 total)

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

### ✅ Repository-Local Storage
- Each repo has its own `.agent-os/cards.sqlite`
- No external services or databases
- Auto-discovers git root for proper isolation
- Safe to commit to version control

### ✅ Simple Card States
- Created: Newly created task
- In Progress: Currently being worked on
- Complete: Finished with summary

### ✅ FastMCP-Based
- Native MCP protocol
- Automatic discovery in Claude Code
- Works with all agent types
- Zero configuration once installed

### ✅ Work Log Tracking
- Comments tied to cards
- Author and timestamp for each entry
- Retrieved with full card data
- Useful for progress tracking and handoff

---

## Testing

All functionality tested and verified:

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

Run tests: `python3 test_server.py`

---

## Integration Path

### Step 1: Install
```bash
pip install -r requirements.txt
```

### Step 2: Add to Claude Code
Edit `~/.claude/settings.json`:
```json
{
  "mcpServers": {
    "task-cards": {
      "command": "python3",
      "args": ["/path/to/task-cards/server.py"],
      "autoConnect": true
    }
  }
}
```

### Step 3: Use in Projects
Add to project `CLAUDE.md`:
```markdown
## Task Management
- Create cards for significant work
- Update status: Created → In Progress → Complete
- Log progress with add_comment
```

---

## Workflow Example

```
User: "Build payment system"
  ↓
Claude orchestrator:
  1. create_card("Stripe integration", priority="high")
  2. create_card("Payment UI", priority="medium")
  3. create_card("Tests", priority="medium")
  ↓
Agent 1:
  1. get_card(stripe_id)
  2. update_card(stripe_id, status="In Progress")
  3. add_comment(stripe_id, "Started implementation")
  4. ... works ...
  5. complete_card(stripe_id, "Stripe integrated and tested")
  ↓
Agent 2:
  1. Similar workflow for other cards
  ↓
Orchestrator reviews:
  - list_cards(status="Complete")
  - Updates memory/summary
  - Returns results to user
```

---

## Architecture Notes

### Database Discovery
- Finds repo root by looking for `.git`
- Creates `.agent-os/` if needed
- Initializes SQLite on first connection
- Thread-safe with sqlite3.Row factory

### Tool Implementation
- Dynamic SQL queries for flexible filtering
- UUID-based card IDs (8-char shortened)
- ISO timestamp tracking (created_at, updated_at)
- Automatic status validation (only 3 valid states)

### Error Handling
- Returns meaningful error messages
- Validates card existence before operations
- Graceful handling of missing fields in updates

---

## What's NOT Included (Intentional Scope)

Still out of scope for the **card subsystem** specifically:

- ❌ Agent ownership/assignment
- ❌ Card dependencies
- ❌ Card-to-file links
- ❌ Priority queues
- ❌ Archive functionality
- ❌ Custom fields

The card subsystem stays intentionally simple. Note that at the **plugin** level,
some of the originally "future" items now exist outside the card schema:
code/database **graph integration** (the 18 `code_*` / `db_*` / `graph_*` tools)
and **multi-agent coordination** (the `agents/` and `skills/` directories).

---

## Files Ready for Use

✅ **server.py** - Production-ready MCP server
✅ **test_server.py** - Full test coverage (verified passing)
✅ **documentation** - README, CLAUDE.md, INTEGRATION.md
✅ **examples** - Usage patterns and configuration templates
✅ **setup** - install.sh for easy onboarding

---

## Next Steps for User

1. Review `README.md` for quick start
2. Review `CLAUDE.md` for detailed tool documentation
3. Follow `INTEGRATION.md` for Claude Code setup
4. Run `test_server.py` to verify installation
5. Start using in projects!

---

## Compliance with Requirements

- ✅ Python FastMCP implementation
- ✅ SQLite database with schema
- ✅ Repository-local storage (`.agent-os/`)
- ✅ 6 MCP tools as specified
- ✅ Card states: Created, In Progress, Complete
- ✅ Comments/work logs for progress tracking
- ✅ Zero external dependencies/services
- ✅ Claude Code integration ready
- ✅ CLAUDE.md integration rules included

All requirements from `outline.md` implemented and tested.
