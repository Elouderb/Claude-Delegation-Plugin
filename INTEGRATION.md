# Integration Guide

How to integrate the Agent OS plugin (and its `task-cards` MCP server) with Claude
Code and your projects.

## Recommended: load as a plugin

```bash
pip install -r mcp/requirements.txt
claude --plugin-dir /path/to/agent-os
```

This auto-discovers the manifest, MCP server (`.mcp.json`), hooks, agents, and
skills. Because `.mcp.json` uses `${CLAUDE_PLUGIN_ROOT}`, nothing needs editing per
machine. Run `claude plugin validate /path/to/agent-os` to confirm it loads.

### Gotcha: developing this plugin inside its own repository

`.mcp.json` intentionally references `${CLAUDE_PLUGIN_ROOT}/mcp/server.py`. That
variable is only injected when the file is loaded **as a plugin**. If you run Claude
Code from inside this repo, the same `.mcp.json` is *also* picked up as a normal
project-scope MCP config, where `${CLAUDE_PLUGIN_ROOT}` is unset — so it expands to
`python3 /mcp/server.py`, which does not exist, and the server fails to start
(`Failed to reconnect to task-cards: -32000`). With the plugin installed too, both
register under the name `task-cards` and collide.

Do **not** change `.mcp.json` to a relative path to "fix" this — the relative path
would then break the plugin for every other project. Instead, suppress the
project-scope copy in this repo only, via `.claude/settings.local.json` (gitignored):

```json
{
  "disabledMcpjsonServers": ["task-cards"]
}
```

This leaves the installed plugin's `task-cards` as the single, working server. The
conflict exists *only* here, in the plugin's own source tree; any other project
resolves `${CLAUDE_PLUGIN_ROOT}` correctly and needs no such override.

> The graph UI port is configurable via `AGENT_OS_GRAPH_PORT` (default `5000`). The
> server reuses an already-running graph server on that port instead of spawning a
> duplicate, so multiple MCP instances (main loop + subagents) don't collide.

## Alternative: per-project MCP config (no plugin)

If you only want the card/graph MCP tools in a single project (without the hooks,
agents, and skills), you can wire just the server:

### 1. Install dependencies

```bash
cd /path/to/agent-os
pip install -r mcp/requirements.txt
```

### 2. Enable for your project

```bash
/path/to/agent-os/mcp/setup-mcp.sh
```

This creates a `.mcp.json` file in your project root that:
- Points to the task-cards server
- Creates project-local storage in `.agent-os/cards.sqlite`
- Can be committed to git so teammates get it automatically

### 3. Restart and Use

```bash
# Restart Claude Code in your project directory
claude
# Task-cards tools are now available!
```

### 4. Optional: Add Rules to CLAUDE.md

In your project's `CLAUDE.md`, add:

```markdown
## Task Management

- Use task cards for all multi-step work
- Create cards before starting significant implementation
- Log progress with add_comment
- Update status: Created → In Progress → Complete
- Complete cards before moving to new work
```

## Usage in Claude Code

Once configured, the MCP server is automatically available to:

- Claude (main loop)
- All subagents
- Workflow agents

### Example: Agent Creates and Uses Cards

```
User: "Build OAuth2 integration"
↓
Claude creates: create_card("Implement OAuth2 flow", priority="high")
↓
Claude starts: update_card(card_id, status="In Progress")
↓
Claude logs: add_comment(card_id, "claude", "JWT implementation complete")
↓
Claude works...
↓
Claude finishes: complete_card(card_id, "OAuth2 fully integrated and tested")
```

## Repository Structure

After first use, each repository will have:

```
your-repo/
  .agent-os/
    cards.sqlite     ← Task data (auto-created)
    config.toml      ← Optional: server config
  CLAUDE.md
  ...other files...
```

The `.agent-os/` directory is:
- ✅ Repository-local (not shared globally)
- ✅ Safe to commit to git (or add to .gitignore)
- ✅ Auto-created on first use

## Configuration

### Repository-Level Config (Optional)

Create `.agent-os/config.toml`:

```toml
[cards]
# Default priority for new cards
default_priority = "medium"

# Auto-transition to "In Progress" when first comment is added
auto_status_on_comment = false

# Archive completed cards older than N days
archive_after_days = 30
```

(Not implemented yet - future enhancement)

## Best Practices

1. **Create before starting**: Always create a card before beginning implementation
2. **Update status**: Move cards through Created → In Progress → Complete
3. **Log progress**: Use comments to track decisions and blockers
4. **Complete thoroughly**: Add a meaningful completion summary
5. **Link work**: Reference card IDs in commit messages or PRs

## Troubleshooting

### Cards not persisting?
- Verify `.agent-os/cards.sqlite` exists in your repository
- Check file permissions

### MCP server not connecting?
- Verify Python path in settings is correct
- Check `requirements.txt` dependencies are installed
- Run `python3 /path/to/server.py` manually to test

### Getting "Card not found"?
- Verify card_id is correct (8 chars, format: `abc12345`)
- Check you're in the right repository

## Multi-Repository Setup

Each repository has its own `.agent-os/cards.sqlite`:

```
repo-a/.agent-os/cards.sqlite    ← Separate database
repo-b/.agent-os/cards.sqlite    ← Separate database
repo-c/.agent-os/cards.sqlite    ← Separate database
```

The MCP server automatically discovers which database to use based on the current working directory's `.git` root.

## Testing

Verify the server works:

```bash
python3 test_server.py
```

Expected output:
```
Testing task-cards MCP server...
✓ Database initialized
✓ All tests passed!
```

## Advanced: Custom Agents

Create a custom agent that uses task cards:

```python
from mcp.client.session import ClientSession

async def my_agent():
    async with ClientSession(process) as session:
        # Tools are automatically available
        result = await session.call_tool("create_card", {
            "title": "My task",
            "description": "Detailed description",
            "priority": "high"
        })
        card_id = result.content[0].text
```

See `example_usage.py` for more patterns.

## Next Steps

1. ✅ Install dependencies
2. ✅ Configure MCP server in Claude Code
3. ✅ Add workflow rules to CLAUDE.md
4. ✅ Start using cards in your projects!

Questions? Check `CLAUDE.md` for detailed documentation.
