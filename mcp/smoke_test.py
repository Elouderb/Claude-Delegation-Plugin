#!/usr/bin/env python3
"""
Fresh-install smoke test for the agent-os MCP plugin.

Catches "only works inside this repo" bugs by installing into a throwaway
venv in a temp dir, exercising the MCP server stdio handshake, the Flask
graph server health check, and a card create/read round-trip, then tearing
everything down even on failure.

Usage
-----
    python3 mcp/smoke_test.py

Environment
-----------
AGENT_OS_GRAPH_PORT   Override the Flask port (default: auto-select a free port)
SMOKE_TIMEOUT         Seconds to wait for servers to become ready (default: 20)
CI                    Set to any non-empty value to enable CI skip-on-failure mode

Exit codes
----------
0  All assertions passed
1  One or more assertions failed (message printed to stderr)
2  Environment problem — server could not be spawned at all (CI-friendly skip)
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import venv
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent  # project root (has mcp/ subdir)
MCP_DIR = REPO_ROOT / "mcp"
REQUIREMENTS = MCP_DIR / "requirements.txt"
SERVER_PY = MCP_DIR / "server.py"
APP_PY = MCP_DIR / "db_tools" / "app.py"

SMOKE_TIMEOUT = int(os.getenv("SMOKE_TIMEOUT", "20"))
CI = bool(os.getenv("CI", ""))

# We pick a free port unless the caller pinned one via AGENT_OS_GRAPH_PORT.
_PINNED_PORT = os.getenv("AGENT_OS_GRAPH_PORT", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fail(msg: str) -> None:
    """Print a failure message and exit non-zero."""
    print(f"\n[SMOKE FAIL] {msg}", file=sys.stderr)
    sys.exit(1)


def _skip(msg: str) -> None:
    """Print a skip notice and exit 2 (CI-friendly: not a test failure)."""
    print(f"[SMOKE SKIP] {msg}", file=sys.stderr)
    sys.exit(2)


def _info(msg: str) -> None:
    print(f"[smoke] {msg}", flush=True)


def _free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: float) -> bool:
    """Poll until something listens on 127.0.0.1:port or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


def _http_get(url: str) -> tuple[int, bytes]:
    """Perform a simple HTTP GET; return (status_code, body_bytes)."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        raise RuntimeError(f"GET {url} failed: {exc}") from exc


# ---------------------------------------------------------------------------
# MCP stdio handshake
# ---------------------------------------------------------------------------

def _build_jsonrpc(method: str, params: dict, id: int = 1) -> bytes:
    # MCP stdio transport: one JSON-RPC message per line, newline-terminated
    # (NOT LSP-style Content-Length framing).
    msg = json.dumps({"jsonrpc": "2.0", "id": id, "method": method, "params": params})
    return msg.encode() + b"\n"


def _read_jsonrpc_response(stream) -> dict:
    """Read one newline-delimited JSON-RPC message from the server's stdout.

    The MCP stdio transport writes each message as a single line of JSON
    terminated by '\\n'. The server must write ONLY JSON-RPC to stdout, so any
    line that is not valid JSON indicates log/banner corruption of the stream —
    which is exactly the failure mode this smoke test exists to catch.
    """
    while True:
        line = stream.readline()
        if not line:
            raise RuntimeError("Server stdout closed unexpectedly before a JSON-RPC reply")
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue  # tolerate incidental blank lines
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Non-JSON line on stdout (log corruption?): {text[:200]!r}"
            ) from exc


# ---------------------------------------------------------------------------
# Main smoke test
# ---------------------------------------------------------------------------

def main() -> None:
    # Validate pre-conditions
    if not SERVER_PY.exists():
        _skip(f"mcp/server.py not found at {SERVER_PY} — cannot run smoke test")
    if not APP_PY.exists():
        _skip(f"mcp/db_tools/app.py not found at {APP_PY} — cannot run smoke test")
    if not REQUIREMENTS.exists():
        _skip(f"mcp/requirements.txt not found at {REQUIREMENTS} — cannot run smoke test")

    # Choose a flask port (avoids collision with an already-running instance)
    flask_port = int(_PINNED_PORT) if _PINNED_PORT else _free_port()

    tmpdir = tempfile.mkdtemp(prefix="agent_os_smoke_")
    venv_dir = Path(tmpdir) / "venv"
    # The server will create .agent-os/ relative to its cwd, so use tmpdir
    work_dir = Path(tmpdir) / "work"
    work_dir.mkdir()
    # Give it a fake .git so repo-root detection lands here
    (work_dir / ".git").mkdir()

    mcp_proc: subprocess.Popen | None = None
    flask_proc: subprocess.Popen | None = None

    def teardown(signum=None, frame=None):
        for proc in [mcp_proc, flask_proc]:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        shutil.rmtree(tmpdir, ignore_errors=True)
        if signum is not None:
            sys.exit(1)

    signal.signal(signal.SIGTERM, teardown)
    signal.signal(signal.SIGINT, teardown)

    try:
        # ------------------------------------------------------------------
        # AC-1a: Install requirements into a throwaway venv
        # ------------------------------------------------------------------
        _info(f"Creating venv at {venv_dir}")
        venv.create(str(venv_dir), with_pip=True)

        pip = venv_dir / "bin" / "pip"
        python = venv_dir / "bin" / "python"

        _info(f"Installing {REQUIREMENTS} ...")
        result = subprocess.run(
            [str(pip), "install", "-r", str(REQUIREMENTS), "-q"],
            capture_output=True, text=True, timeout=180
        )
        if result.returncode != 0:
            _fail(
                f"pip install failed (exit {result.returncode}):\n"
                f"stdout: {result.stdout[-500:]}\nstderr: {result.stderr[-500:]}"
            )
        _info("Requirements installed OK")

        # ------------------------------------------------------------------
        # AC-1b: Start the MCP server — Flask graph server will launch as a
        # child of the MCP server on flask_port.
        # We set AGENT_OS_GRAPH_PORT so the server uses our chosen port.
        # ------------------------------------------------------------------
        _info(f"Starting MCP server (flask will bind :{flask_port}) ...")
        env = {
            **os.environ,
            "AGENT_OS_GRAPH_PORT": str(flask_port),
            # Suppress any PYTHONPATH from the outer env that might pull in
            # the host-level mcp package instead of the venv's copy.
            "PYTHONPATH": "",
            "VIRTUAL_ENV": str(venv_dir),
            "PATH": str(venv_dir / "bin") + os.pathsep + os.environ.get("PATH", ""),
        }
        mcp_proc = subprocess.Popen(
            [str(python), str(SERVER_PY)],
            cwd=str(work_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )

        # Give the server a moment to start up before we try to handshake
        time.sleep(1)

        if mcp_proc.poll() is not None:
            stderr_out = mcp_proc.stderr.read().decode(errors="replace")
            _fail(
                f"MCP server exited immediately (rc={mcp_proc.returncode}).\n"
                f"stderr: {stderr_out[-800:]}"
            )

        # ------------------------------------------------------------------
        # AC-1b (continued): stdio handshake — send initialize, read response
        # ------------------------------------------------------------------
        _info("Sending MCP initialize request ...")
        init_request = _build_jsonrpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke-test", "version": "0.0.1"},
        })

        try:
            mcp_proc.stdin.write(init_request)
            mcp_proc.stdin.flush()
        except BrokenPipeError:
            stderr_out = mcp_proc.stderr.read().decode(errors="replace")
            _fail(f"MCP server stdin closed (crashed?).\nstderr: {stderr_out[-800:]}")

        # Read the response with a timeout guard
        import threading

        response: dict | None = None
        read_error: str | None = None

        def _read_response():
            nonlocal response, read_error
            try:
                response = _read_jsonrpc_response(mcp_proc.stdout)
            except Exception as exc:
                read_error = str(exc)

        reader = threading.Thread(target=_read_response, daemon=True)
        reader.start()
        reader.join(timeout=SMOKE_TIMEOUT)

        if reader.is_alive():
            _fail(
                f"MCP server did not respond to initialize within {SMOKE_TIMEOUT}s "
                "(possible log corruption on stdout or server deadlock)"
            )

        if read_error:
            stderr_so_far = ""
            try:
                import select
                r, _, _ = select.select([mcp_proc.stderr], [], [], 0)
                if r:
                    stderr_so_far = mcp_proc.stderr.read(2000).decode(errors="replace")
            except Exception:
                pass
            _fail(
                f"JSON-RPC read error: {read_error}\n"
                f"Server stderr (partial): {stderr_so_far}"
            )

        if response is None:
            _fail("No response received from MCP server (unknown error)")

        # Validate the response is a well-formed JSON-RPC reply
        if "jsonrpc" not in response:
            _fail(f"Response missing 'jsonrpc' field: {response}")
        if response.get("jsonrpc") != "2.0":
            _fail(f"Expected jsonrpc==2.0, got: {response.get('jsonrpc')!r}")
        if "result" not in response and "error" not in response:
            _fail(f"Response missing both 'result' and 'error': {response}")
        if "error" in response:
            _fail(f"MCP initialize returned error: {response['error']}")

        _info(f"MCP stdio handshake OK (serverInfo: {response.get('result', {}).get('serverInfo')})")

        # ------------------------------------------------------------------
        # AC-1c: Flask graph server health check
        # ------------------------------------------------------------------
        _info(f"Waiting for Flask graph server on port {flask_port} ...")
        if not _wait_for_port(flask_port, SMOKE_TIMEOUT):
            stderr_out = ""
            try:
                import select
                r, _, _ = select.select([mcp_proc.stderr], [], [], 0)
                if r:
                    stderr_out = mcp_proc.stderr.read(4000).decode(errors="replace")
            except Exception:
                pass
            _fail(
                f"Flask server did not bind port {flask_port} within {SMOKE_TIMEOUT}s.\n"
                f"MCP server stderr (partial): {stderr_out}"
            )

        health_url = f"http://127.0.0.1:{flask_port}/health"
        _info(f"GET {health_url}")
        status_code, body = _http_get(health_url)
        if status_code != 200:
            _fail(f"/health returned HTTP {status_code}, expected 200. Body: {body[:200]!r}")

        try:
            health_data = json.loads(body)
        except json.JSONDecodeError:
            _fail(f"/health body is not JSON: {body[:200]!r}")

        if health_data.get("status") != "ok":
            _fail(f"/health JSON missing status=ok: {health_data}")

        _info(f"Flask /health OK: {health_data}")

        # ------------------------------------------------------------------
        # AC-1d: Card create + read-back via the MCP card path (direct call,
        # not via stdio, because the card tools are already exercised through
        # the imported Python module; the important thing is the DB path works
        # inside the throwaway venv/dir).  We call via a small subprocess so
        # the path truly exercises the installed environment.
        # ------------------------------------------------------------------
        _info("Testing card create/read via Python subprocess ...")
        card_test_script = (
            "import sys, json\n"
            "sys.path.insert(0, '')\n"  # workdir is CWD
            f"sys.path.insert(0, {str(SERVER_PY.parent)!r})\n"
            "import server\n"
            "server.ensure_agent_os()\n"
            "card = server.create_card(title='Smoke test card', priority='low')\n"
            "assert 'card_id' in card, f'No card_id in: {card}'\n"
            "fetched = server.get_card(card['card_id'])\n"
            "assert fetched['title'] == 'Smoke test card', f'Title mismatch: {fetched}'\n"
            "assert fetched['status'] == 'Created', f'Status mismatch: {fetched}'\n"
            "print(json.dumps({'card_id': card['card_id'], 'status': fetched['status']}))\n"
        )

        # Run with a separate AGENT_OS_GRAPH_PORT so this subprocess does NOT
        # launch a second Flask server on the same port (it would reuse the
        # running one via _port_in_use guard in server.py).
        card_env = {
            **env,
            "AGENT_OS_GRAPH_PORT": str(flask_port),  # port already in use -> reuse
        }
        card_result = subprocess.run(
            [str(python), "-c", card_test_script],
            cwd=str(work_dir),
            capture_output=True, text=True, timeout=30,
            env=card_env,
        )
        if card_result.returncode != 0:
            _fail(
                f"Card create/read subprocess failed (rc={card_result.returncode}):\n"
                f"stdout: {card_result.stdout[-500:]}\nstderr: {card_result.stderr[-500:]}"
            )

        # Extract the JSON line printed by the script (stderr may contain log lines)
        card_json_line = card_result.stdout.strip().splitlines()[-1]
        try:
            card_info = json.loads(card_json_line)
        except json.JSONDecodeError:
            _fail(
                f"Card test script stdout is not JSON: {card_result.stdout[-300:]!r}\n"
                f"stderr: {card_result.stderr[-300:]}"
            )

        _info(f"Card round-trip OK: {card_info}")

        # ------------------------------------------------------------------
        # All assertions passed
        # ------------------------------------------------------------------
        _info("All smoke test assertions passed.")
        print("\n[SMOKE PASS] Fresh-install smoke test: EXIT 0", flush=True)

    except SystemExit:
        raise
    except Exception as exc:
        _fail(f"Unexpected exception: {exc}")
    finally:
        teardown()


if __name__ == "__main__":
    main()
