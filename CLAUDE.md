# Task Cards MCP Server

Jira-style task management system for Claude Code and AI agents. Repository-local, SQLite-backed, built on the `mcp` Python package.

## What This Is

A lightweight task/card management system designed as an MCP server. It provides:

- **Repository-local storage**: All task data lives in `.agent-os/cards.sqlite` within your repo
- **Simple card lifecycle**: Created → In Progress → Complete
- **Work logs**: Comment system for tracking progress
- **Flexible filtering**: Query cards by status or priority
- **Card subsystem has zero external dependencies**: pure SQLite, no external services. The optional database-graph subsystem has extra dependencies (`pyodbc` plus a live SQL Server) and is not required to use cards.

## Installation

```bash
# Install dependencies (core: mcp, flask, python-dotenv)
pip install -r mcp/requirements.txt
```

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
- `installer/install.sh` - Installation helper
- `CLAUDE.md` - This documentation
- `.agent-os/cards.sqlite` - Repository-local database (created on first run)

## Notes

- Cards are repository-local; each repo has its own database
- The MCP server auto-initializes the database on first connection
- The card subsystem requires no external services; the optional database-graph subsystem does (`pyodbc` + a live SQL Server)
- Safe to commit `.agent-os/` to version control (or use `.gitignore`)
