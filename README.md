# Agent OS

A repository-local "operating system" for agentic development in Claude Code, packaged as a Claude Code plugin (version `0.2.5`). It bundles task tracking, code/database knowledge graphs, lifecycle hooks, and a delegation-oriented set of agents and skills.

## What's in the plugin

| Component | Location | Purpose |
|-----------|----------|---------|
| **Task cards MCP server** | `mcp/server.py` | Jira-style cards in a repo-local SQLite DB (`.agent-os/cards.sqlite`). 6 card tools. |
| **Graph query tools** | `mcp/server.py` | 18 read-oriented MCP tools over the Graphify code graph and the database graph (24 MCP tools total: 6 card + 18 graph/code/db). |
| **Database graph builder** | `mcp/db_tools/` | Builds a graph of a Microsoft SQL Server schema (`build_db_graph.py` + `build_graph_html.py`). Optional; requires a SQL Server connection. |
| **Graph-sync hooks** | `hooks/hooks.json`, `scripts/` | Keep the repository graph fresh and protect generated files. See `hooks/README.md`. |
| **Agents** | `agents/` | `implementer`, `complex-implementer` (opus / high-effort, for repo-wide or complex changes), `frontend-engineer` (UI), `codebase-consultant` (read-only repo investigator other agents can delegate to), `code-reviewer`, `security-reviewer`, `test-engineer`, `verification-engineer` (runs the real app / browser), `database-engineer`, `research-planner`. |
| **Skills** | `skills/` | 24 workflow skills for cards, planning, review, testing, codebase investigation, verification, and graph discipline. |

The MCP server is named `task-cards` (it exposes both the card tools and the graph tools).

## Installation

This is a Claude Code plugin. The recommended path is to install it via the bundled local marketplace so the manifest (`.claude-plugin/plugin.json`), MCP server (`.mcp.json`), hooks, agents, and skills are all discovered automatically.

```bash
# 1. Install the MCP server's Python dependencies
pip install -r mcp/requirements.txt

# 2. Register the local marketplace defined in .claude-plugin/marketplace.json
#    (marketplace name: agent-os-local) and install the plugin:
/plugin marketplace add /path/to/agent-os
/plugin install agent-os@agent-os-local

# 3. Then /reload-plugins (or restart Claude Code). Verify the server with /mcp.
```

`.mcp.json` launches the server with `python3 ${CLAUDE_PLUGIN_ROOT}/mcp/server.py`, so the plugin is portable across machines once dependencies are installed.

> **For local development of the plugin**, you can live-load the directory instead:
> `claude --plugin-dir /path/to/agent-os`. This is a dev convenience, not the
> primary install path.

`graphify` must be on your `PATH` for the code/database graph features and the
graph-sync hooks (which run `graphify update .`) to work.

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
| Task cards | `.agent-os/cards.sqlite` |
| Database graph | `.agent-os/db/` |
| Code graph | `graphify-out/` |

### Optional: database graph

The `db_*` graph tools and `mcp/db_tools/` connect to a Microsoft SQL Server instance. This subsystem has optional extra dependencies — `pyodbc`, a live SQL Server, and `DB_CONNECTION_STRING` set in `.env`. Copy `.env.example` to `.env` and set `DB_CONNECTION_STRING`. The `.env` file is gitignored and must never be committed. If you don't use the database tools, you can ignore this entirely.

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

## Testing

```bash
cd mcp && python3 test_server.py   # the 6 card tools (8/8)
python3 -m pytest mcp/tests/       # full CI suite: code graph, Flask routes, hooks, DB resilience
```

See `mcp/example_usage.py` for runnable usage patterns.

## Status

The card system is functional and tested. The database-graph subsystem is optional and requires `pyodbc`, a live SQL Server, and a `DB_CONNECTION_STRING` in `.env`. See `CHANGELOG.md` for the change history.
