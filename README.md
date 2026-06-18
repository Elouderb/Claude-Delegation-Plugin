# Agent OS

A repository-local "operating system" for agentic development in Claude Code, packaged as a Claude Code plugin. It bundles task tracking, code/database knowledge graphs, lifecycle hooks, and a delegation-oriented set of agents and skills.

## What's in the plugin

| Component | Location | Purpose |
|-----------|----------|---------|
| **Task cards MCP server** | `mcp/server.py` | Jira-style cards in a repo-local SQLite DB (`.agent-os/cards.sqlite`). 6 card tools. |
| **Graph query tools** | `mcp/server.py` | 18 read-oriented MCP tools over the Graphify code graph and the database graph. |
| **Database graph builder** | `mcp/db_tools/` | Builds a graph of a Microsoft SQL Server schema (`build_db_graph.py` + `build_graph_html.py`). Optional; requires a SQL Server connection. |
| **Graph-sync hooks** | `hooks/hooks.json`, `scripts/` | Keep the repository graph fresh and protect generated files. See `hooks/README.md`. |
| **Agents** | `agents/` | `implementer`, `code-reviewer`, `test-engineer`, `database-engineer`, `research-planner`. |
| **Skills** | `skills/` | 17 workflow skills for cards, planning, review, testing, and graph discipline. |

The MCP server is named `task-cards` (it exposes both the card tools and the graph tools).

## Installation

This is a Claude Code plugin. The recommended path is to load it as a plugin so the manifest (`.claude-plugin/plugin.json`), MCP server (`.mcp.json`), hooks, agents, and skills are all discovered automatically.

```bash
# 1. Install the MCP server's Python dependencies
pip install -r mcp/requirements.txt

# 2. Load the plugin directory in Claude Code:
claude --plugin-dir /path/to/agent-os
```

`.mcp.json` launches the server with `python3` and `${CLAUDE_PLUGIN_ROOT}`, so the plugin is portable across machines once dependencies are installed.

### Optional: database graph

The `db_*` graph tools and `mcp/db_tools/` connect to a Microsoft SQL Server instance. Copy `.env.example` to `.env` and set `DB_CONNECTION_STRING`. The `.env` file is gitignored and must never be committed. If you don't use the database tools, you can ignore this entirely.

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

- `CLAUDE.md` — detailed tool reference and the recommended agent operating model.
- `INTEGRATION.md` — project setup and workflow rules.
- `hooks/README.md` — how the graph-sync hooks behave.
- `NEW_MCP_TOOLING.md` — specification for the 18 graph tools.
- `outline.md` — original task-cards specification.

## Testing

```bash
cd mcp && python3 test_server.py   # exercises the 6 card tools (8/8)
```

## Status

The card system is functional and tested. The database-graph subsystem is optional and requires SQL Server + an `.env`. See `mcp/FIXES_APPLIED.md` for the change history.
