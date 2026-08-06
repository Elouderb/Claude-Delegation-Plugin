#!/usr/bin/env python3
"""Verify a task-cards MCP install with a real MCP handshake over stdio.

This is the check that actually proves an install works (WINDOWS.md Section 5.5):
it reads the *generated* ``.mcp.json``, spawns the EXACT configured command, and
speaks the Model Context Protocol over the child's stdio -- ``initialize``, then
``notifications/initialized``, then ``tools/list`` -- and asserts the expected
tool count.  A green result confirms the interpreter path, the installed
dependencies, and the generated config all agree, in one step.

It speaks raw JSON-RPC 2.0 (newline-delimited, UTF-8) directly over the pipe and
imports nothing beyond the standard library -- in particular it does NOT import
the ``mcp`` package, so the verifier itself has no dependency on what it is
verifying.  Cross-platform: it uses reader threads rather than ``select`` (which
does not work on pipes on Windows), so both installers can run it.

Exit code 0 = pass (exactly ``--expected-tools`` tools); non-zero = fail.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

# installer/verify_install.py -> the plugin root is this file's parent's parent.
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MCP_JSON = PLUGIN_ROOT / ".mcp.json"
DEFAULT_SERVER_NAME = "task-cards"
# 6 card + 7 shared-graph + 6 database-graph + 5 code-graph + 3 memory tools.
DEFAULT_EXPECTED_TOOLS = 27
PROTOCOL_VERSION = "2024-11-05"

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_EOF = object()


class HandshakeError(Exception):
    """Raised when the MCP handshake cannot be completed."""


class HandshakeResult:
    def __init__(
        self,
        ok: bool,
        tool_count: int,
        tool_names: list[str],
        expected: int,
        error: str | None = None,
        stderr: str = "",
    ) -> None:
        self.ok = ok
        self.tool_count = tool_count
        self.tool_names = tool_names
        self.expected = expected
        self.error = error
        self.stderr = stderr


def _expandvars(text: str, env: dict[str, str]) -> str:
    """Expand ``${VAR}`` / ``$VAR`` using ``env`` (mirrors Claude Code's
    ``${CLAUDE_PLUGIN_ROOT}`` expansion without mutating the global environment).
    Unknown variables are left untouched."""

    def repl(match: "re.Match[str]") -> str:
        name = match.group(1) or match.group(2)
        return env.get(name, match.group(0))

    return _VAR_RE.sub(repl, text)


def load_command(
    mcp_json_path: Path,
    server_name: str,
    plugin_root: Path,
) -> tuple[str, list[str], dict[str, str]]:
    """Read the generated .mcp.json and return the fully-expanded
    ``(command, args, child_env)`` for ``server_name``.

    ``${CLAUDE_PLUGIN_ROOT}`` is expanded to ``plugin_root`` (as Claude Code does
    before spawning), and that value is also exported into the child environment
    so any late expansion inside the server resolves identically.
    """
    if not mcp_json_path.exists():
        raise HandshakeError(f".mcp.json not found: {mcp_json_path}")

    config = json.loads(mcp_json_path.read_text(encoding="utf-8"))
    servers = config.get("mcpServers") or {}
    if server_name not in servers:
        raise HandshakeError(
            f"server '{server_name}' not in {mcp_json_path} "
            f"(found: {sorted(servers)})"
        )
    entry = servers[server_name]

    child_env = dict(os.environ)
    child_env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    # Force UTF-8 in the child so its stdio and any file writes are UTF-8 even
    # on a cp1252 Windows console (WINDOWS.md Section 3/4).
    child_env["PYTHONIOENCODING"] = "utf-8"
    # Suppress any PYTHONPATH from the outer env that might pull in a host-level
    # mcp package instead of the venv's copy (mirrors smoke_test): the verifier
    # must exercise the SAME interpreter+deps Claude Code will use, not whatever
    # a stray PYTHONPATH shadows in — otherwise it can pass against packages the
    # real launch never sees, or fail a correct install.
    child_env["PYTHONPATH"] = ""
    child_env.update({k: str(v) for k, v in (entry.get("env") or {}).items()})

    command = _expandvars(str(entry["command"]), child_env)
    args = [_expandvars(str(a), child_env) for a in entry.get("args", [])]
    return command, args, child_env


def check_graphify(plugin_root: Path) -> str | None:
    """Best-effort check that the ``graphify`` console script will resolve.

    Card 43ba7d28: ``graphifyy`` (the PyPI package providing the ``graphify``
    console script the graphify skill, the graph_refresh hooks, and the graph
    UI all depend on) was an undeclared, purely-ambient dependency -- a fresh
    install had no path to it at all, and the only symptom was a silent,
    repeated runtime hook warning ("Graphify executable not found"). This
    turns that into a single, clear install-time signal instead.

    Mirrors the resolution order of ``mcp/graph_io.py``'s and
    ``scripts/hook_common.py``'s ``resolve_graphify()`` (env override, then a
    console script beside the venv interpreter, then ``PATH``) closely enough
    to give an accurate signal, without importing either module (this file
    stays stdlib-only by design -- see the module docstring). Returns ``None``
    when ``graphify`` resolves, or a short human-readable diagnostic when it
    does not. Never raises.
    """
    override = os.environ.get("AGENT_OS_GRAPHIFY_EXECUTABLE")
    if override:
        if Path(override).exists() or shutil.which(override):
            return None
        return f"AGENT_OS_GRAPHIFY_EXECUTABLE={override!r} does not resolve"

    venv_python = (
        plugin_root
        / ".venv"
        / ("Scripts" if os.name == "nt" else "bin")
        / ("python.exe" if os.name == "nt" else "python")
    )
    exe_dir = venv_python.parent
    for name in ("graphify.exe", "graphify"):
        if (exe_dir / name).exists():
            return None

    if shutil.which("graphify"):
        return None

    return (
        f"'graphify' was not found beside {venv_python} or on PATH -- the "
        "graphify skill, graph_refresh hooks, and graph UI will warn "
        "\"Graphify executable not found\" until it is installed "
        "(pip install -r mcp/graph_requirements.txt, or re-run the "
        "installer without --no-graph / -NoGraph)."
    )


def _reader_thread(stream: Any, out_queue: "queue.Queue[Any]") -> None:
    try:
        for line in iter(stream.readline, ""):
            out_queue.put(line)
    finally:
        out_queue.put(_EOF)


def _drain_thread(stream: Any, sink: list[str]) -> None:
    try:
        for line in iter(stream.readline, ""):
            sink.append(line)
    except Exception:  # pragma: no cover - defensive
        pass


def handshake(
    command: str,
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    expected_tools: int = DEFAULT_EXPECTED_TOOLS,
    timeout: float = 30.0,
) -> HandshakeResult:
    """Spawn ``command args`` and run initialize + tools/list over stdio."""
    stderr_lines: list[str] = []
    try:
        proc = subprocess.Popen(
            [command, *args],
            cwd=str(cwd) if cwd else None,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            # Never let a stray non-UTF-8 byte on the child's pipes crash the
            # reader with UnicodeDecodeError — degrade to a replacement char.
            errors="replace",
            bufsize=1,
        )
    except (FileNotFoundError, OSError) as exc:
        # A stale/removed interpreter path or an otherwise un-spawnable command:
        # report a clean FAIL instead of letting the raw OSError escape as a
        # traceback (there is no child, hence no stderr to show).
        return HandshakeResult(
            ok=False,
            tool_count=0,
            tool_names=[],
            expected=expected_tools,
            error=f"could not spawn {command!r}: {exc}",
            stderr="",
        )
    out_queue: "queue.Queue[Any]" = queue.Queue()
    threading.Thread(
        target=_reader_thread, args=(proc.stdout, out_queue), daemon=True
    ).start()
    err_thread = threading.Thread(
        target=_drain_thread, args=(proc.stderr, stderr_lines), daemon=True
    )
    err_thread.start()

    def stderr_tail() -> str:
        # Let the child finish writing/closing stderr so the tail we report is
        # complete even when it died mid-handshake, then collect it.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        err_thread.join(timeout=2)
        return "".join(stderr_lines)

    def send(message: dict[str, Any]) -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            # The server exited before/while we wrote (bad interpreter, an import
            # crash on startup): surface it as a HandshakeError so the handler
            # below returns a clean FAIL carrying the captured server-stderr tail.
            raise HandshakeError(
                "server closed its input before the handshake completed "
                f"(it likely exited on startup): {exc}"
            ) from exc

    def await_response(expected_id: int) -> dict[str, Any]:
        while True:
            try:
                item = out_queue.get(timeout=timeout)
            except queue.Empty:
                raise HandshakeError(
                    f"timed out after {timeout}s waiting for response id={expected_id}"
                )
            if item is _EOF:
                raise HandshakeError("server closed stdout before responding")
            line = item.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # The server logs to stderr; a non-JSON stdout line is unexpected
                # but non-fatal -- skip it rather than aborting.
                continue
            if message.get("id") == expected_id:
                return message

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "verify_install", "version": "1.0"},
                },
            }
        )
        init = await_response(1)
        if "error" in init:
            raise HandshakeError(f"initialize failed: {init['error']}")

        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = await_response(2)
        if "error" in listed:
            raise HandshakeError(f"tools/list failed: {listed['error']}")

        tools = (listed.get("result") or {}).get("tools") or []
        names = sorted(t.get("name", "?") for t in tools)
        ok = len(names) == expected_tools
        return HandshakeResult(
            ok=ok,
            tool_count=len(names),
            tool_names=names,
            expected=expected_tools,
            error=None if ok else f"expected {expected_tools} tools, got {len(names)}",
            stderr="".join(stderr_lines),
        )
    except HandshakeError as exc:
        return HandshakeResult(
            ok=False,
            tool_count=0,
            tool_names=[],
            expected=expected_tools,
            error=str(exc),
            stderr=stderr_tail(),
        )
    finally:
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                proc.kill()


def verify(
    mcp_json_path: Path = DEFAULT_MCP_JSON,
    *,
    server_name: str = DEFAULT_SERVER_NAME,
    plugin_root: Path = PLUGIN_ROOT,
    expected_tools: int = DEFAULT_EXPECTED_TOOLS,
    cwd: Path | None = None,
    timeout: float = 30.0,
) -> HandshakeResult:
    """Load the generated config and run the handshake against it."""
    command, args, env = load_command(mcp_json_path, server_name, plugin_root)
    return handshake(
        command,
        args,
        cwd=cwd if cwd is not None else plugin_root,
        env=env,
        expected_tools=expected_tools,
        timeout=timeout,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a task-cards MCP install via a real MCP handshake.",
    )
    parser.add_argument("--mcp-json", type=Path, default=DEFAULT_MCP_JSON)
    parser.add_argument("--server-name", default=DEFAULT_SERVER_NAME)
    parser.add_argument("--plugin-root", type=Path, default=PLUGIN_ROOT)
    parser.add_argument("--expected-tools", type=int, default=DEFAULT_EXPECTED_TOOLS)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    try:
        result = verify(
            args.mcp_json,
            server_name=args.server_name,
            plugin_root=args.plugin_root,
            expected_tools=args.expected_tools,
            timeout=args.timeout,
        )
    except HandshakeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    # Soft check, independent of the handshake result: it never changes the
    # exit code (graphify is not required for the MCP handshake itself -- the
    # graph tools still register, they just degrade at call time), but it
    # turns a silent runtime hook warning into a visible install-time one.
    graphify_issue = check_graphify(args.plugin_root)
    if graphify_issue:
        print(f"WARNING: {graphify_issue}", file=sys.stderr)

    if result.ok:
        print(
            f"PASS: MCP handshake OK -- {result.tool_count} tools "
            f"(expected {result.expected})."
        )
        return 0

    print(f"FAIL: {result.error}", file=sys.stderr)
    if result.tool_names:
        print(f"  tools reported: {', '.join(result.tool_names)}", file=sys.stderr)
    if result.stderr.strip():
        tail = "\n".join(result.stderr.strip().splitlines()[-15:])
        print(f"  server stderr (tail):\n{tail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
