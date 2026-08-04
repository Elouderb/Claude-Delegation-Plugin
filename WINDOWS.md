# Windows Compatibility

Field notes from getting `agent-os` running on Windows 10 (PowerShell 5.1, no WSL).
Everything below was hit in practice, not inferred. Verified 2026-07-30 against
plugin 0.2.6, Python 3.12.10, graphify 0.9.31.

Audience: the development agent maintaining this plugin. The goal is that a fresh
Windows install works without hand-patching, and that future changes don't
regress it.

---

## The one-line summary

The plugin assumed three things that are false on Windows: that `python3` exists,
that POSIX venv layout (`bin/`) is universal, and that the default text encoding
is UTF-8. Each assumption produced a distinct, non-obvious failure.

---

## 1. `python3` does not exist on Windows

**Symptom:** the `task-cards` MCP server never starts; every hook is a silent no-op.

**Cause:** `.mcp.json` and all eight commands in `hooks/hooks.json` invoked
`python3`. On Windows, `python3` resolves to
`%LOCALAPPDATA%\Microsoft\WindowsApps\python3.exe` — a Microsoft Store
*app-execution-alias stub*, not an interpreter. It is on `PATH` by default, so
`command -v python3` / `shutil.which("python3")` both succeed. It is worse than a
missing binary: presence checks pass and the failure surfaces later and elsewhere.

**Applied fix:** both files now point at an explicit interpreter path.

**Note for the maintainer:** this is the only change in the set that is *not*
portable — see §5 for the recommended permanent form. Do not simply swap
`python3` → `python`; bare `python` is absent on many Linux distributions, so
that trades one platform's breakage for another's.

## 2. Console-script resolution (`graphify`)

**Symptom:** `Graphify executable not found` on every lifecycle hook, even with
graphify correctly installed.

**Cause:** four sites resolved the bare name `graphify` through `PATH`. When the
plugin runs from its own virtual environment, that venv's `Scripts/` (Windows) or
`bin/` (POSIX) directory is *not* on `PATH` — only the interpreter is invoked by
absolute path. An `AGENT_OS_GRAPHIFY_EXECUTABLE` override existed but only covered
one of the four sites.

**Applied fix:** a `resolve_graphify()` helper — override env var, then a console
script beside `sys.executable`, then `PATH`:

| File | Site |
|---|---|
| `scripts/hook_common.py` | new helper; `graphify_command()` uses it |
| `scripts/sync_repo_graph.py` | health probe (was reporting a false negative) |
| `mcp/graph_io.py` | new helper |
| `mcp/shared_graph_tools.py` | `graph_refresh()` |
| `mcp/db_tools/app.py` | `refresh_repo()` route; local copy — this module runs as a standalone process and cannot import `graph_io` |

This fix is OS-agnostic: it checks `graphify.exe` then `graphify`, so it resolves
`Scripts/graphify.exe` and `bin/graphify` alike. **Keep it that way** — any new
call site should use the helper rather than a bare name.

## 3. Default text encoding is cp1252, not UTF-8

This is the highest-value lesson in this document, because it fails *silently and
destructively*.

**Symptom:** the DB graph page rendered blank. `db_graph.json`, `.md`, and
`.graphml` were all correct; `db_graph.html` was **0 bytes**. The route served it
as HTTP 200, so it looked like a rendering bug rather than a build failure.

**Cause:** `pyvis.network.write_html()` calls `open(name, "w+")` with no
`encoding`. On Windows that is cp1252. Any character outside Latin-1 anywhere in
the schema raises `UnicodeEncodeError` — *after* `open` has already truncated the
file. The failure mode is a zero-byte artifact, not a missing one.

**Applied fix** in `mcp/db_tools/build_graph_html.py` — generate the markup and
write it explicitly:

```python
html = network.generate_html(str(OUTPUT_FILE), notebook=False)
OUTPUT_FILE.write_text(html, encoding="utf-8")
```

**Rule going forward:** every text read/write in this codebase must pass
`encoding="utf-8"` explicitly. `build_db_graph.py`'s `atomic_write_text()` already
does this correctly (and writes temp-then-replace, which is why the JSON survived
while the HTML did not) — treat it as the reference implementation. Never rely on
the platform default. This applies to `open()`, `Path.read_text()`,
`Path.write_text()`, and `subprocess.run(..., text=True)`.

The same class of bug affects **stdout**, not just files — see §4.

---

## 4. Known-broken, not yet fixed

Listed in the order I would fix them.

**`mcp/test_server.py` — fix this first.** Lines 33, 113, 117 print `✓`/`✗`.
Under a cp1252 console the *failure handler itself* dies with
`UnicodeEncodeError`, masking the real error and reporting a spurious failure.
It cost real debugging time. Either drop to ASCII markers or force the stream to
UTF-8 at startup. Until then, run it as
`PYTHONIOENCODING=utf-8 python mcp/test_server.py`.

**`mcp/smoke_test.py` — cannot run on Windows at all.** Lines 196–197 hardcode
`venv_dir / "bin" / "pip"` and `venv_dir / "bin" / "python"`. Needs
`"Scripts" if os.name == "nt" else "bin"`, and `pip.exe`/`python.exe`.

**`installer/install.sh` — no Windows entry point.** Line 12 is
`if ! command -v python3 &> /dev/null` — the §1 failure, in the installer itself.
The whole script is bash-only. See §5.

**`.env.example` — stale ODBC driver.** Line 4 pins
`DRIVER={ODBC Driver 17 for SQL Server}`. Current Windows installs ship
**Driver 18**; copying the example verbatim fails with driver-not-found. Driver 18
also defaults to encryption on, so the existing
`Encrypt=yes;TrustServerCertificate=yes` pair is required, not optional. Suggest
documenting `pyodbc.drivers()` as the way to discover what is actually installed.

---

## 5. Recommendation: handling the Python environment

The current state works but is not portable: `.mcp.json` and `hooks/hooks.json`
contain a hardcoded `.venv/Scripts/python.exe`. That path is Windows-only.
`${CLAUDE_PLUGIN_ROOT}` is expanded by Claude Code, but there is no conditional
expansion — one JSON file cannot express both layouts.

### Why a bundled venv is the right call regardless

Ambient-interpreter resolution is what caused most of this session's failures.
A plugin-local venv fixes several problems at once:

- The interpreter is invoked by absolute path, so the Store stub is bypassed.
- Console scripts (`graphify`) sit beside that interpreter and become resolvable
  without touching `PATH` (§2).
- Dependencies cannot collide with whatever the user's system Python holds.

One caution learned the hard way: **do not put the venv's `Scripts/` directory on
`PATH`.** It contains `python.exe` and would shadow the user's system Python
globally. Resolve relative to `sys.executable` instead.

### Recommended: a generated launcher, not a checked-in path

Keep `.venv/` in the repo tree (already gitignored) but stop hardcoding the
interpreter path in version-controlled config.

1. **Bootstrap script, per platform.** Ship `installer/install.ps1` alongside
   `install.sh`. Both should: locate a suitable interpreter, create `.venv`,
   `pip install -r mcp/requirements.txt`, and then **generate** `.mcp.json` and
   `hooks/hooks.json` from templates with the correct interpreter path
   substituted. Generated config is gitignored; templates are committed. This
   removes the portability regression entirely — no OS branch survives in
   version control.

2. **Interpreter discovery on Windows.** Prefer the `py` launcher:
   `py -3.12 -m venv .venv`. It is the only reliable discovery mechanism —
   it found Python 3.12.10 on this machine when `python3` was a stub and `python`
   was an unrelated 3.11 install on another drive. Fall back to `python3` on
   POSIX. Never probe for `python3` on Windows.

3. **Single source of truth for the interpreter path.** One helper:

   ```python
   VENV_PYTHON = (
       PLUGIN_ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")
       / ("python.exe" if os.name == "nt" else "python")
   )
   ```

   Anything that spawns a subprocess should use it. `refresh_db()` in
   `mcp/db_tools/app.py` already does the right thing by spawning
   `sys.executable`; prefer that pattern over a resolved name wherever the child
   is a Python script.

4. **Make requirements complete.** `pyodbc` and `pyvis` are commented out as
   optional in `mcp/requirements.txt`, but the DB-graph web UI is unusable
   without `pyvis`, and its failure mode is the zero-byte page from §3. Either
   move them into a documented `[db]` extra that the installer can select, or
   have `refresh_db()` check for `pyvis` up front and return a clear error.
   `python-dotenv` is genuinely core (`app.py` imports it at module level) and is
   already correctly listed.

5. **Verify with a real handshake.** The check that actually proves the install is
   an MCP `initialize` + `tools/list` over stdio against the exact configured
   command — it confirms interpreter, dependencies, and config in one step.
   A working install returns 24 tools [now 27 — memory tools added in 0.2.x]. Worth adding to both installers.

---

## 6. Companion process lifecycle (graph server on :5000)

**Symptom (M2 field report, 2026-08-04):** after `/reload-plugins` on Windows the
old graph-server child survives, keeps port 5000 bound, and — in the worst case —
the new MCP server's **card tools become inaccessible**.

**Cause.** Two Windows gaps, now both fixed in `mcp/graph_server.py` /
`mcp/sync_server.py` / `mcp/server.py`:

1. *No kill-on-parent-exit.* The orphan-reaping used on Linux is
   `PR_SET_PDEATHSIG`, which is Linux-only; Windows has no `preexec_fn` at all, so a
   companion simply orphaned when the MCP server died. **Fixed** by assigning every
   spawned companion (graph server *and* the optional sync worker) to a Windows
   **Job Object** created with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (ctypes,
   stdlib). The parent holds the job handle, so when it dies by *any* means the OS
   closes the handle and kills the children. Requires Windows 8+ (nested jobs);
   Win10+ is the target. On any failure it logs and continues (no crash).
2. *Startup coupled to the companion.* A held/stale/foreign :5000 could delay or
   break server startup and take the card tools down with it. **Fixed** by (a) fully
   isolating companion startup from card-tool registration — card tools always
   register and serve even if both companions fail — and (b) strictly time-bounded
   stale-port recovery at spawn: a healthy agent-os server on the port is reused; our
   own *wedged* server (identified via a per-port owner file + a `/health` nonce) is
   terminated and the port reclaimed; a foreign process makes us relocate to the next
   free port (unless `AGENT_OS_GRAPH_PORT` pins it, which we respect). Startup never
   hangs on the port.

### Hotfix for older installs (PowerShell)

On a pre-fix install you may still find an orphaned process holding :5000 after a
reload. To find and kill it (PowerShell 5.1+, no admin needed for your own
processes):

```powershell
# Show what is listening on :5000, then kill it.
Get-NetTCPConnection -LocalPort 5000 -State Listen |
  Select-Object -ExpandProperty OwningProcess |
  ForEach-Object { Stop-Process -Id $_ -Force }
```

`netstat`/`taskkill` fallback (any Windows, including where `Get-NetTCPConnection`
is unavailable):

```powershell
netstat -ano | findstr :5000     # note the PID in the last column
taskkill /PID <pid> /F
```

To sidestep the port entirely without killing anything, pin a different port for the
session: `setx AGENT_OS_GRAPH_PORT 5051` (new shells) or `$env:AGENT_OS_GRAPH_PORT=5051`
(current shell). The graph UI then serves from that port; card tools are unaffected
either way.

**Note for the maintainer:** the kill-on-parent-exit mechanism is unit-testable on
the `windows-latest` CI leg (`mcp/tests/test_job_object.py` — spawn a child via the
job helper, kill the parent, assert the child dies); it skips on POSIX, mirroring how
`test_pdeathsig.py` skips on Windows. The startup-isolation and stale-port logic run
cross-platform (`mcp/tests/test_startup_isolation.py`,
`mcp/tests/test_stale_port_recovery.py`).

---

## Appendix: files changed for Windows support

Eight files, all verified working:

```
.mcp.json                          interpreter path
hooks/hooks.json                   interpreter path (8 hook commands)
scripts/hook_common.py             resolve_graphify() + shutil import
scripts/sync_repo_graph.py         health probe uses resolve_graphify()
mcp/graph_io.py                    resolve_graphify()
mcp/shared_graph_tools.py          graph_refresh() uses it
mcp/db_tools/app.py                refresh_repo() uses it (local copy)
mcp/db_tools/build_graph_html.py   explicit UTF-8 write
```

Of these, only `.mcp.json` and `hooks/hooks.json` are Windows-specific; §5 is the
plan for removing even those. The rest are correct on every platform and should be
kept as-is.

Environment used: Python 3.12.10 venv at `.venv/` with `mcp` 1.29.0, `flask`
3.1.3, `python-dotenv` 1.2.2, `graphifyy` 0.9.31 (PyPI name is `graphifyy`; the
console script it installs is `graphify`), `pyodbc` 5.3.0, `pyvis` 0.3.2.

---

## Not a Windows issue, but found alongside

`graph_search_nodes` in `mcp/shared_graph_tools.py` defaults to exact match
(`query == node_id or query == label`), and its `node_type` filter matches
graphify's `file_type`, whose only values are `code` and `document`. An agent
filtering `node_type="function"` gets zero results with `warnings: []` — an
empty, confident answer indistinguishable from "no such symbol." This caused a
false "the graph is down" report. Consider returning a warning naming the types
actually present when a filtered search yields nothing.
