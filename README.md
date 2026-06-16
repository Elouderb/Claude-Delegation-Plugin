# Task Cards MCP Server

A lightweight, repository-local task management system for Claude Code and AI agents.

**Quick Start:**

```bash
# Install dependencies
pip install -r requirements.txt

# Enable in your project (one-time per project)
/path/to/task-cards/setup-mcp.sh

# Restart Claude Code and start using!
```

**Features:**
- 📦 Repository-local SQLite database (`.agent-os/cards.sqlite`)
- 🏷️ Simple card workflow: Created → In Progress → Complete
- 📝 Work logs via comments
- 🔍 Filter by status or priority
- 🚀 Zero external services

**Available Tools:**
- `create_card` - Create new task
- `list_cards` - Query tasks (with filters)
- `get_card` - Retrieve full card + comments
- `update_card` - Modify card fields
- `add_comment` - Log work
- `complete_card` - Mark done with summary

**Integration:**
Add to Claude Code MCP settings:
```
Command: python3 /path/to/server.py
```

See `CLAUDE.md` for detailed documentation and workflow examples.
