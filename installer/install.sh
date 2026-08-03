#!/usr/bin/env bash
# Bootstrap installer for the agent-os plugin (POSIX / Linux / macOS).
#
# Creates a plugin-local .venv, installs the core Python dependencies (plus any
# selected extras), GENERATES .mcp.json and hooks/hooks.json from the committed
# templates with the venv interpreter's absolute path, and verifies the result
# with a real MCP handshake. The generated config is git-ignored; only templates
# are committed (see WINDOWS.md Section 5). On Windows use installer/install.ps1.
#
# Usage:
#   installer/install.sh [--with-db] [--with-memory] [--all-extras]
#                        [--no-extras] [--non-interactive|-y] [-h|--help]
#
# Non-interactive selection (for CI) can also be driven by environment:
#   AGENT_OS_NONINTERACTIVE=1   never prompt (implied by CI=1 or a non-tty stdin)
#   AGENT_OS_INSTALL_DB=1       install the database extra (pyodbc, networkx, pyvis)
#   AGENT_OS_INSTALL_MEMORY=1   install the memory extra (sqlite-vec, ...)
#   AGENT_OS_PYTHON=python3.12  interpreter used to create the venv (default python3)

set -euo pipefail

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$INSTALLER_DIR/.." && pwd)"
MCP_DIR="$PLUGIN_ROOT/mcp"
VENV_DIR="$PLUGIN_ROOT/.venv"
VENV_PY="$VENV_DIR/bin/python"

WITH_DB="${AGENT_OS_INSTALL_DB:-0}"
WITH_MEMORY="${AGENT_OS_INSTALL_MEMORY:-0}"
NONINTERACTIVE="${AGENT_OS_NONINTERACTIVE:-0}"
BASE_PYTHON="${AGENT_OS_PYTHON:-python3}"

# CI or a non-interactive stdin implies "never prompt".
if [ -n "${CI:-}" ] || [ ! -t 0 ]; then
    NONINTERACTIVE=1
fi

usage() {
    # Print the leading comment block (after the shebang) up to the first
    # non-comment line, stripped of the leading "# ". awk over an explicit line
    # range keeps --help robust to future header edits — the old sed '3,25p'
    # overshot the header and printed raw `set -euo pipefail` + code.
    awk 'NR==1 && /^#!/ {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "${BASH_SOURCE[0]}"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --with-db)          WITH_DB=1 ;;
        --with-memory)      WITH_MEMORY=1 ;;
        --all-extras)       WITH_DB=1; WITH_MEMORY=1 ;;
        --no-extras)        WITH_DB=0; WITH_MEMORY=0; NONINTERACTIVE=1 ;;
        -y|--non-interactive) NONINTERACTIVE=1 ;;
        -h|--help)          usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
    esac
    shift
done

echo "Installing agent-os plugin into: $PLUGIN_ROOT"

# --- 1. Interpreter discovery (POSIX: python3 is the right gate) --------------
# NOTE: never probe 'python3' on Windows -- there it is a Microsoft Store stub
# (WINDOWS.md Section 1). That is why Windows has its own install.ps1.
if ! command -v "$BASE_PYTHON" >/dev/null 2>&1; then
    echo "ERROR: '$BASE_PYTHON' not found. Install Python 3 (or set AGENT_OS_PYTHON)." >&2
    exit 1
fi
echo "Base interpreter: $("$BASE_PYTHON" --version 2>&1) ($BASE_PYTHON)"

# --- 2. Create the plugin-local venv -----------------------------------------
if [ ! -x "$VENV_PY" ]; then
    echo "Creating virtual environment at $VENV_DIR ..."
    "$BASE_PYTHON" -m venv "$VENV_DIR"
    # Upgrade pip ONLY when we just created the venv. Re-running the installer
    # against an existing venv should not hit PyPI to re-upgrade pip every time —
    # that stalls (or fails) on an offline machine with a working venv already.
    echo "Upgrading pip ..."
    "$VENV_PY" -m pip install --quiet --upgrade pip
else
    echo "Reusing existing virtual environment at $VENV_DIR"
fi

# --- 3. Core dependencies ----------------------------------------------------
echo "Installing core dependencies (mcp/requirements.txt) ..."
"$VENV_PY" -m pip install --quiet -r "$MCP_DIR/requirements.txt"

# --- 4. Optional extras ------------------------------------------------------
prompt_yes_no() {
    # $1 = prompt text; returns 0 for yes, 1 for no.
    local reply
    printf '%s [y/N] ' "$1"
    read -r reply || reply=""
    case "$reply" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

if [ "$NONINTERACTIVE" != "1" ]; then
    if [ "$WITH_DB" != "1" ] && prompt_yes_no "Install the database extra (pyodbc, networkx, pyvis)?"; then
        WITH_DB=1
    fi
    if [ "$WITH_MEMORY" != "1" ] && prompt_yes_no "Install the central-memory extra (sqlite-vec, sentence-transformers)?"; then
        WITH_MEMORY=1
    fi
fi

if [ "$WITH_DB" = "1" ]; then
    echo "Installing database extra (mcp/db_requirements.txt) ..."
    "$VENV_PY" -m pip install --quiet -r "$MCP_DIR/db_requirements.txt"
fi
if [ "$WITH_MEMORY" = "1" ]; then
    echo "Installing central-memory extra (mcp/memory_requirements.txt) ..."
    "$VENV_PY" -m pip install --quiet -r "$MCP_DIR/memory_requirements.txt"
fi

# --- 5. Generate .mcp.json + hooks/hooks.json from templates -----------------
# Run with the venv interpreter so sys.executable IS the venv python -- the
# single source of truth for the interpreter path (WINDOWS.md Section 5.3).
echo "Generating .mcp.json and hooks/hooks.json ..."
"$VENV_PY" "$PLUGIN_ROOT/scripts/generate_config.py"

# --- 6. Verify with a real MCP handshake -------------------------------------
echo "Verifying install (MCP initialize + tools/list) ..."
if "$VENV_PY" "$PLUGIN_ROOT/installer/verify_install.py"; then
    echo ""
    echo "[OK] Installation complete."
else
    echo "" >&2
    echo "[FAIL] Handshake verification failed. See the errors above." >&2
    exit 1
fi

echo ""
echo "To use in Claude Code (plugin install path):"
echo "  1. /plugin marketplace add $PLUGIN_ROOT"
echo "  2. /plugin install agent-os@agent-os-local"
echo "  3. /reload-plugins (or restart Claude Code); verify with /mcp"
echo ""
echo "Task cards are stored in .agent-os/cards.sqlite in each repository."
