#!/bin/bash
# Installation and setup script for task-cards MCP server

set -e

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_DIR="$INSTALLER_DIR/../mcp"

echo "Installing task-cards MCP server..."

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is required but not found"
    exit 1
fi

# Install dependencies
echo "Installing Python dependencies..."
pip install -q -r "$MCP_DIR/requirements.txt"

# Make server executable
chmod +x "$MCP_DIR/server.py"

echo "✓ Installation complete!"
echo ""
echo "To install in Claude Code:"
echo "1. Register the local marketplace (run once):"
echo "   /plugin marketplace add /path/to/agent-os"
echo "2. Install the plugin:"
echo "   /plugin install agent-os@agent-os-local"
echo "3. Reload: /reload-plugins (or restart Claude Code)"
echo "4. Verify with /mcp — you should see 'task-cards' listed"
echo ""
echo "Task cards are stored in .agent-os/cards.sqlite in each repository."
