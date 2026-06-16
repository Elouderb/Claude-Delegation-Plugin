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
echo "To use this MCP server in Claude Code:"
echo "1. Add to your Claude Code MCP settings"
echo "2. Configure the command: python3 $MCP_DIR/server.py"
echo "3. Task cards will be stored in .agent-os/cards.sqlite in your repository"
