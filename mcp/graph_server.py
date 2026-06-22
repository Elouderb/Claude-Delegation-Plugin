"""
Flask graph server lifecycle management.

Handles starting/stopping the graph UI subprocess, port collision avoidance,
and repo registration for the multi-repo graph URL scheme.
"""

import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

from graph_io import get_repo_root, log

# Flask app process (initialized in setup)
flask_process: Optional[subprocess.Popen] = None

_REPO_REGISTRY = Path.home() / ".agent-os" / "active_repos.json"


def _graph_port() -> int:
    """Port for the Flask graph UI. Override with AGENT_OS_GRAPH_PORT (default 5000)."""
    return int(os.getenv("AGENT_OS_GRAPH_PORT") or "5000")


def _register_repo(repo_root: Path) -> str:
    """Write repo_root into the shared registry and return its URL slug.

    The slug is the repo directory name. If two repos share a name the
    parent directory is prepended (e.g. ``parent-reponame``) so slugs stay
    unique and human-readable.
    """
    slug = repo_root.name
    _REPO_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    try:
        registry: dict = json.loads(_REPO_REGISTRY.read_text()) if _REPO_REGISTRY.exists() else {}
    except Exception:
        registry = {}

    existing = registry.get(slug)
    if existing and existing != str(repo_root):
        slug = f"{repo_root.parent.name}-{repo_root.name}"

    registry[slug] = str(repo_root)
    try:
        _REPO_REGISTRY.write_text(json.dumps(registry, indent=2))
    except Exception:
        pass
    return slug


def _port_in_use(port: int) -> bool:
    """Return True if something is already listening on 127.0.0.1:<port>."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _probe_health(port: int, timeout: float = 2.0) -> bool:
    """Probe http://127.0.0.1:<port>/health and confirm it is the agent-os graph server.

    Returns True only when the endpoint responds HTTP 200 with JSON that contains
    ``{"status": "ok", "repos": ...}``.  Any other response — wrong status code,
    missing keys, network error — returns False so the caller knows the port is
    NOT held by our server.

    Uses stdlib ``http.client`` only; no third-party dependencies.
    """
    import http.client

    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        if resp.status != 200:
            return False
        body = resp.read(4096).decode("utf-8", errors="replace")
        data = json.loads(body)
        # Must be our server: "status" == "ok" and "repos" key present.
        return data.get("status") == "ok" and "repos" in data
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()


# PR_SET_PDEATHSIG: prctl option that asks the kernel to deliver a signal to
# THIS (calling) process when its parent dies. Value is stable across Linux
# (since 2.1.57; see `man 2 prctl` / <linux/prctl.h>).
_PR_SET_PDEATHSIG = 1


def _make_pdeathsig_preexec(parent_pid: int):
    """Build a preexec_fn that couples the child's lifetime to ``parent_pid`` (Linux only).

    The returned callable runs in the child after fork() but before exec(). It:
      1. Best-effort asks the kernel (PR_SET_PDEATHSIG) to SIGTERM this process
         when its parent — the spawning MCP server — dies by ANY means (SIGTERM,
         SIGKILL, crash, force-reap). This reaps the orphaned :5000 graph server
         instead of leaving it reparented to PID 1 (the failure mode this fixes).
      2. Re-checks os.getppid(): if the real parent already died in the tiny
         window between fork() and prctl(), we have been reparented to init, so
         the pdeathsig would fire against the wrong parent and never arrive —
         exit immediately to avoid an orphan (closes the fork/parent-death race).

    The prctl call is wrapped defensively so an unexpected libc/ctypes failure
    cannot abort the whole spawn; the getppid race-check still runs regardless.

    IMPORTANT: PR_SET_PDEATHSIG is attached to the spawning *thread*, not the
    whole process — the signal fires when the thread that called fork() exits,
    even if the process lives on. The caller MUST therefore spawn from a thread
    that lives for the full process lifetime (the main thread). `_spawn_graph_server`
    enforces this with a main-thread guard; do NOT call this from a transient
    worker thread (e.g. one created by ``anyio.to_thread.run_sync``), or the
    worker's exit would prematurely SIGTERM the live graph server mid-session.
    """
    def _preexec() -> None:
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
        except Exception:
            # Best effort: if prctl is unavailable we fall back to the existing
            # atexit-based cleanup path. Do not crash the child's launch.
            pass
        # Race guard: parent may have died between fork() and prctl() above.
        if os.getppid() != parent_pid:
            os._exit(0)
    return _preexec


def _spawn_graph_server(app_path: Path, repo_root: Path, port: int) -> "subprocess.Popen[str]":
    """Launch the Flask graph server subprocess and return the Popen handle.

    On Linux the child is coupled to this process via PR_SET_PDEATHSIG so the
    graph server dies with the MCP server that owns it (no orphaned :5000
    processes). On non-Linux platforms ``preexec_fn`` is not attached and we keep
    relying on the existing atexit cleanup path — behavior is unchanged there.

    The pdeathsig coupling is attached only when we are on the main thread,
    because PR_SET_PDEATHSIG keys off the spawning thread's lifetime (see
    _make_pdeathsig_preexec). Spawning from a transient worker thread would make
    that thread's exit kill a healthy graph server, so we fall back to the
    atexit path in that case rather than risk a premature kill.
    """
    parent_pid = os.getpid()
    on_main_thread = threading.current_thread() is threading.main_thread()
    use_pdeathsig = sys.platform == "linux" and on_main_thread
    preexec = _make_pdeathsig_preexec(parent_pid) if use_pdeathsig else None
    proc = subprocess.Popen(
        [sys.executable, str(app_path)],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env={**os.environ, "PORT": str(port)},
        preexec_fn=preexec,
    )
    log(f"Graph server started (PID: {proc.pid}) at {app_path}")
    return proc


def start_graph_server():
    """Start the Flask graph server in a subprocess.

    Claude Code spawns a separate MCP server process for the main loop and for
    each subagent, so multiple instances would otherwise collide on the graph
    UI's single TCP port and leak orphan processes.

    Port-reuse logic (AC #1):
      If the port is already in use, probe /health to confirm the listener is
      actually OUR graph server before reusing it.  If the port is held by a
      foreign or unhealthy process, we log clearly and do not spawn.

    Respawn logic (AC #2 + #3):
      If we previously spawned a child and it has since exited, surface its
      captured stderr and then respawn a fresh instance.

    The port is configurable via AGENT_OS_GRAPH_PORT (default 5000).
    """
    global flask_process
    try:
        repo_root = get_repo_root()
        slug = _register_repo(repo_root)
        port = _graph_port()

        app_path = Path(__file__).resolve().parent / "db_tools" / "app.py"
        if not app_path.exists():
            log(f"WARNING: Graph server app not found at {app_path}")
            return

        # --- Respawn check (AC #2 + #3) -----------------------------------
        # If we own a child process and it has already exited, surface its
        # stderr and clear the handle so we fall through to spawn a new one.
        # poll() once. A non-None code means the child has already terminated,
        # so its pipe buffers are bounded and communicate() reaches EOF without
        # a timeout — no risk of blocking on a full pipe.
        exit_code = flask_process.poll() if flask_process is not None else None
        if flask_process is not None and exit_code is not None:
            log(f"WARNING: Previously-spawned graph server (PID {flask_process.pid}) "
                f"exited with code {exit_code}; will respawn.")
            try:
                _stdout, _stderr = flask_process.communicate()
                if _stderr:
                    log(f"Graph server stderr before exit: {_stderr[:500]}")
                if _stdout:
                    log(f"Graph server stdout before exit: {_stdout[:200]}")
            except Exception:
                pass
            flask_process = None

        # --- Already have a live owned child --------------------------------
        if flask_process is not None:
            # Our child is still running; nothing to do.
            log(f"Graph server already owned (PID {flask_process.pid}); reusing.")
            log(f"Graphs available at http://localhost:{port}/{slug}/ (code_graph, db_graph, task_cards)")
            return

        # --- Port already in use by someone else? ---------------------------
        if _port_in_use(port):
            if _probe_health(port):
                log(f"Graph server already running on port {port} (health probe OK); reusing it.")
                log(f"Graphs available at http://localhost:{port}/{slug}/ (code_graph, db_graph, task_cards)")
                return
            else:
                log(f"WARNING: Port {port} is in use but /health probe failed — "
                    f"a foreign or unhealthy process holds that port. "
                    f"The graph UI will NOT be available until the port is freed. "
                    f"Set AGENT_OS_GRAPH_PORT to a different port to work around this.")
                return

        # --- Spawn fresh -------------------------------------------------------
        flask_process = _spawn_graph_server(app_path, repo_root, port)
        log(f"Graphs available at http://localhost:{port}/{slug}/ (code_graph, db_graph, task_cards)")

    except Exception as e:
        log(f"ERROR starting graph server: {e}")


def _check_graph_server_health():
    """Check if graph server is still running; respawn it if it crashed.

    Called by ``load_database_graph`` and ``load_code_graph`` before every
    graph access.  If the child has died this function:
      1. Surfaces the captured stderr (AC #2).
      2. Clears ``flask_process`` (AC #3 prerequisite).
      3. Calls ``start_graph_server()`` to respawn immediately (AC #3).
    """
    global flask_process
    exit_code = flask_process.poll() if flask_process else None
    if flask_process and exit_code is not None:
        # Process has exited — surface its output first. The child is already
        # dead, so communicate() drains the bounded pipe buffers and returns at
        # EOF without needing a timeout.
        try:
            stdout, stderr = flask_process.communicate()
            if stderr:
                log(f"WARNING: Graph server crashed (exit {exit_code}). stderr: {stderr[:500]}")
            if stdout:
                log(f"Graph server output: {stdout[:200]}")
        except Exception:
            pass
        flask_process = None
        log("WARNING: Graph server process exited unexpectedly; attempting respawn.")
        # Respawn so the graph UI recovers without needing a full MCP restart.
        start_graph_server()


def shutdown_flask():
    """Terminate the Flask graph server subprocess."""
    global flask_process
    if flask_process:
        try:
            flask_process.terminate()
            flask_process.wait(timeout=5)
            log("Graph server process terminated")
        except subprocess.TimeoutExpired:
            flask_process.kill()
            log("Graph server process killed")
        except Exception as e:
            log(f"ERROR shutting down graph server: {e}")
