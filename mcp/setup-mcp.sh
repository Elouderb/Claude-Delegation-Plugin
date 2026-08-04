#!/bin/bash
# Add the task-cards MCP server to the current project WITHOUT installing the
# agent-os plugin (the "per-project MCP config" path from INTEGRATION.md).
#
# It writes a project-scope .mcp.json that launches mcp/server.py with the
# plugin's OWN venv interpreter (absolute path) -- NOT a bare `python3`, which
# lacks the server's dependencies and, on Windows, resolves to a Microsoft Store
# execution-alias stub that silently no-ops the server (WINDOWS.md Section 1).
# Run installer/install.sh first to create that venv.

set -e

# task-cards root = mcp/ (this script's dir); the plugin root is its parent.
TASK_CARDS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_PATH="$TASK_CARDS_ROOT/server.py"
PLUGIN_ROOT="$(cd "$TASK_CARDS_ROOT/.." && pwd)"
VENV_PY="$PLUGIN_ROOT/.venv/bin/python"

# The server needs its dependencies, and only the plugin venv has them. Fail
# with a clear pointer rather than writing a config that launches a broken
# interpreter (the old `python3` default).
if [ ! -x "$VENV_PY" ]; then
    echo "Error: plugin venv interpreter not found at:" >&2
    echo "  $VENV_PY" >&2
    echo "Run the bootstrap installer first:" >&2
    echo "  $PLUGIN_ROOT/installer/install.sh   (Windows: installer/install.ps1)" >&2
    exit 1
fi

# Check if we're in a git repository
if ! git rev-parse --git-dir > /dev/null 2>&1; then
    echo "Error: Not in a git repository"
    exit 1
fi

# Get the git root
GIT_ROOT="$(git rev-parse --show-toplevel)"

# Create .mcp.json in the project root
MCP_FILE="$GIT_ROOT/.mcp.json"

cat > "$MCP_FILE" << EOF
{
  "mcpServers": {
    "task-cards": {
      "command": "$VENV_PY",
      "args": ["$SERVER_PATH"]
    }
  }
}
EOF

# Protect the card database from accidental git tracking.
# .agent-os/.gitignore contains a single wildcard so git ignores everything
# under .agent-os/ regardless of the project's root .gitignore.
AGENT_OS_DIR="$GIT_ROOT/.agent-os"
AGENT_OS_GITIGNORE="$AGENT_OS_DIR/.gitignore"
mkdir -p "$AGENT_OS_DIR"
if [ ! -f "$AGENT_OS_GITIGNORE" ]; then
    printf '*\n' > "$AGENT_OS_GITIGNORE"
fi

echo "[OK] Task-cards MCP server configured in $GIT_ROOT"
echo "  Interpreter: $VENV_PY"
echo "  Cards will be stored in: $GIT_ROOT/.agent-os/cards.sqlite"
echo "  The .agent-os/ directory is git-ignored by default (do NOT commit the live card DB)."
echo "  Committing cards.sqlite risks losing cards on git reset/checkout/rebase."
echo "  To share cards with teammates, use export -- not a git commit."
echo ""
echo "Next steps:"
echo "  1. Restart Claude Code in this directory"
echo "  2. Task-cards tools will be available"
echo ""
echo "Note: this .mcp.json embeds an absolute interpreter path specific to THIS"
echo "machine, so it should NOT be committed. Each teammate runs this script"
echo "(after installer/install.sh) to generate their own."
