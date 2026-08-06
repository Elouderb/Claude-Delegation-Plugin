#Requires -Version 5.1
<#
.SYNOPSIS
    Bootstrap installer for the agent-os plugin on Windows (PowerShell 5.1+).

.DESCRIPTION
    Creates a plugin-local .venv, installs the core Python dependencies (plus any
    selected extras), GENERATES .mcp.json and hooks\hooks.json from the committed
    templates with the venv interpreter's absolute path, and verifies the result
    with a real MCP handshake. Generated config is git-ignored; only templates are
    committed (see WINDOWS.md Section 5).

    Interpreter discovery uses the 'py' launcher (py -3.12, then py -3). It NEVER
    probes 'python3': on Windows that name resolves to a Microsoft Store
    execution-alias stub, not a real interpreter (WINDOWS.md Section 1).

.PARAMETER WithDb
    Install the database extra (pyodbc, networkx, pyvis).

.PARAMETER WithMemory
    Install the central-memory extra (sqlite-vec, pysqlite3-binary, sentence-transformers).

.PARAMETER WithGraph
    Install the code-graph extra (graphifyy). Installed by default; this switch
    is only useful to override a prior -NoGraph / AGENT_OS_INSTALL_GRAPH=0.

.PARAMETER AllExtras
    Install both the database and central-memory extras.

.PARAMETER NoExtras
    Install neither the database nor central-memory extra, and do not prompt.
    Does not affect the code-graph extra -- use -NoGraph for that.

.PARAMETER NoGraph
    Skip the code-graph extra (graphifyy). It is installed by DEFAULT (unlike
    -WithDb / -WithMemory above) because the code graph is a headline,
    always-on plugin capability, not an opt-in extra.

.PARAMETER NonInteractive
    Never prompt (implied by the CI environment variable). Use for automation.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File installer\install.ps1 -AllExtras -NonInteractive
#>
[CmdletBinding()]
param(
    [switch]$WithDb,
    [switch]$WithMemory,
    [switch]$WithGraph,
    [switch]$AllExtras,
    [switch]$NoExtras,
    [switch]$NoGraph,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'

# --- Paths -------------------------------------------------------------------
$InstallerDir = $PSScriptRoot
$PluginRoot   = (Resolve-Path (Join-Path $InstallerDir '..')).Path
$McpDir       = Join-Path $PluginRoot 'mcp'
$VenvDir      = Join-Path $PluginRoot '.venv'
$VenvPy       = Join-Path $VenvDir 'Scripts\python.exe'

Write-Host "Installing agent-os plugin into: $PluginRoot"

# --- Resolve extras / interactivity ------------------------------------------
$doDb     = [bool]$WithDb
$doMemory = [bool]$WithMemory
# Default-ON, unlike the two above: the code graph (graphify skill,
# graph_refresh hooks, Flask graph UI) is a headline plugin capability, not an
# opt-in extra. Opt out with -NoGraph / AGENT_OS_INSTALL_GRAPH=0.
$doGraph  = $true
if ($AllExtras) { $doDb = $true; $doMemory = $true }

if ($env:AGENT_OS_INSTALL_DB -eq '1')     { $doDb = $true }
if ($env:AGENT_OS_INSTALL_MEMORY -eq '1') { $doMemory = $true }
if ($env:AGENT_OS_INSTALL_GRAPH -eq '0')  { $doGraph = $false }
if ($WithGraph) { $doGraph = $true }

$interactive = -not $NonInteractive
# -NoExtras must FORCE both extras off AND stop prompting, overriding any
# AGENT_OS_INSTALL_* env vars set above — exactly matching `install.sh
# --no-extras` (which zeroes WITH_DB/WITH_MEMORY after the env defaults). Placed
# after the env checks so it wins. -NoExtras deliberately does NOT touch
# $doGraph (see -NoGraph, the separate opt-out for the default-on graph extra).
if ($NoExtras) { $doDb = $false; $doMemory = $false; $interactive = $false }
if ($NoGraph) { $doGraph = $false }
if ($env:AGENT_OS_NONINTERACTIVE -eq '1') { $interactive = $false }
if ($env:CI) { $interactive = $false }
if (-not [Environment]::UserInteractive) { $interactive = $false }

# --- 1. Interpreter discovery via the 'py' launcher --------------------------
function Find-PythonBase {
    # Explicit override wins (may be a full path to python.exe).
    if ($env:AGENT_OS_PYTHON) {
        return ,@($env:AGENT_OS_PYTHON)
    }
    $candidates = @(
        @('py', '-3.12'),
        @('py', '-3')
    )
    foreach ($cand in $candidates) {
        $probeArgs = @()
        if ($cand.Count -gt 1) { $probeArgs += $cand[1..($cand.Count - 1)] }
        $probeArgs += @('-c', 'import sys')
        # Under $ErrorActionPreference='Stop', a native command that writes to
        # stderr (e.g. the py launcher noting an unavailable version) is promoted
        # to a TERMINATING NativeCommandError — which would discard a perfectly
        # good launcher and mis-report "No suitable Python found". Drop to
        # 'Continue' just around the probe, merge stderr into the captured output
        # (2>&1) so it can't leak, and gate strictly on $LASTEXITCODE.
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $probeOk = $false
        try {
            $probeOut = & $cand[0] @probeArgs 2>&1
            $probeOk = ($LASTEXITCODE -eq 0)
        } catch {
            # launcher/selector genuinely not present; try the next candidate.
            $probeOk = $false
        } finally {
            $ErrorActionPreference = $prevEAP
        }
        # Capture probe output into a variable and discard it, so it can never
        # pollute this function's return stream (which must be the candidate only).
        $null = $probeOut
        if ($probeOk) { return ,$cand }
    }
    return $null
}

$baseCmd = Find-PythonBase
if ($null -eq $baseCmd) {
    Write-Error "No suitable Python found. Install Python 3 (the 'py' launcher: 'py -3.12' or 'py -3'), or set AGENT_OS_PYTHON to a python.exe path."
    exit 1
}
$baseExe  = $baseCmd[0]
$baseArgs = @()
if ($baseCmd.Count -gt 1) { $baseArgs = $baseCmd[1..($baseCmd.Count - 1)] }

$verOut = & $baseExe @baseArgs '--version'
Write-Host "Base interpreter: $verOut ($baseExe $($baseArgs -join ' '))"

# --- 2. Create the plugin-local venv -----------------------------------------
if (-not (Test-Path $VenvPy)) {
    Write-Host "Creating virtual environment at $VenvDir ..."
    & $baseExe @baseArgs '-m' 'venv' $VenvDir
    if ($LASTEXITCODE -ne 0) { Write-Error "venv creation failed."; exit 1 }
    # Upgrade pip ONLY on fresh creation — re-running against an existing venv
    # should not hit PyPI to re-upgrade pip every time (offline machines stall).
    Write-Host "Upgrading pip ..."
    & $VenvPy -m pip install --quiet --upgrade pip
    if ($LASTEXITCODE -ne 0) { Write-Error "pip upgrade failed."; exit 1 }
} else {
    Write-Host "Reusing existing virtual environment at $VenvDir"
}

# --- 3. Core dependencies ----------------------------------------------------
Write-Host "Installing core dependencies (mcp\requirements.txt) ..."
& $VenvPy -m pip install --quiet -r (Join-Path $McpDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Write-Error "core dependency install failed."; exit 1 }

# --- 3b. Code-graph extra (default ON; opt out with -NoGraph) ---------------
# Unlike the db/memory extras below, this is not prompted -- it is installed
# unless explicitly disabled, because a fresh install without it silently
# degrades the graphify skill, graph_refresh hooks, and the graph UI (card
# 43ba7d28: "Graphify executable not found" on a clean machine).
if ($doGraph) {
    Write-Host "Installing code-graph extra (mcp\graph_requirements.txt) ..."
    & $VenvPy -m pip install --quiet -r (Join-Path $McpDir 'graph_requirements.txt')
    if ($LASTEXITCODE -ne 0) { Write-Error "code-graph extra install failed."; exit 1 }
} else {
    Write-Host "Skipping code-graph extra (-NoGraph): the graphify skill, graph_refresh hooks, and graph UI will report 'Graphify executable not found' until 'pip install -r mcp\graph_requirements.txt' is run manually."
}

# --- 4. Optional extras ------------------------------------------------------
if ($interactive) {
    if (-not $doDb) {
        $ans = Read-Host "Install the database extra (pyodbc, networkx, pyvis)? [y/N]"
        if ($ans -match '^[Yy]') { $doDb = $true }
    }
    if (-not $doMemory) {
        $ans = Read-Host "Install the central-memory extra (sqlite-vec, sentence-transformers)? [y/N]"
        if ($ans -match '^[Yy]') { $doMemory = $true }
    }
}

if ($doDb) {
    Write-Host "Installing database extra (mcp\db_requirements.txt) ..."
    & $VenvPy -m pip install --quiet -r (Join-Path $McpDir 'db_requirements.txt')
    if ($LASTEXITCODE -ne 0) { Write-Error "database extra install failed."; exit 1 }
}
if ($doMemory) {
    Write-Host "Installing central-memory extra (mcp\memory_requirements.txt) ..."
    & $VenvPy -m pip install --quiet -r (Join-Path $McpDir 'memory_requirements.txt')
    if ($LASTEXITCODE -ne 0) { Write-Error "central-memory extra install failed."; exit 1 }
}

# --- 5. Generate .mcp.json + hooks\hooks.json from templates -----------------
# Run with the venv interpreter so sys.executable IS the venv python -- the
# single source of truth for the interpreter path (WINDOWS.md Section 5.3).
Write-Host "Generating .mcp.json and hooks\hooks.json ..."
& $VenvPy (Join-Path $PluginRoot 'scripts\generate_config.py')
if ($LASTEXITCODE -ne 0) { Write-Error "config generation failed."; exit 1 }

# --- 6. Verify with a real MCP handshake -------------------------------------
Write-Host "Verifying install (MCP initialize + tools/list) ..."
& $VenvPy (Join-Path $PluginRoot 'installer\verify_install.py')
if ($LASTEXITCODE -ne 0) {
    Write-Error "Handshake verification failed. See the errors above."
    exit 1
}

Write-Host ""
Write-Host "[OK] Installation complete."
Write-Host ""
Write-Host "To use in Claude Code (plugin install path):"
Write-Host "  1. /plugin marketplace add $PluginRoot"
Write-Host "  2. /plugin install agent-os@agent-os-local"
Write-Host "  3. /reload-plugins (or restart Claude Code); verify with /mcp"
Write-Host ""
Write-Host "Task cards are stored in .agent-os\cards.sqlite in each repository."
