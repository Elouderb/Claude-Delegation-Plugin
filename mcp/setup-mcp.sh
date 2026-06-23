#!/bin/bash
# Add task-cards MCP server to current project

set -e

# Get the absolute path to this script's directory (task-cards root)
TASK_CARDS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_PATH="$TASK_CARDS_ROOT/server.py"

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
      "command": "python3",
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

echo "✓ Task-cards MCP server configured in $GIT_ROOT"
echo "  Cards will be stored in: $GIT_ROOT/.agent-os/cards.sqlite"
echo "  The .agent-os/ directory is git-ignored by default (do NOT commit the live card DB)."
echo "  Committing cards.sqlite risks losing cards on git reset/checkout/rebase."
echo "  To share cards with teammates, use export — not a git commit."
echo ""
echo "Next steps:"
echo "  1. Restart Claude Code in this directory"
echo "  2. Task-cards tools will be available"
echo "  3. Commit .mcp.json to share with teammates"
