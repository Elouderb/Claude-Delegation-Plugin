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

echo "✓ Task-cards MCP server configured in $GIT_ROOT"
echo "  Cards will be stored in: $GIT_ROOT/.agent-os/cards.sqlite"
echo ""
echo "Next steps:"
echo "  1. Restart Claude Code in this directory"
echo "  2. Task-cards tools will be available"
echo "  3. Commit .mcp.json to share with teammates"
