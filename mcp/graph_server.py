"""
Flask graph server lifecycle management.

Handles starting/stopping the graph UI subprocess, port collision avoidance,
and repo registration for the multi-repo graph URL scheme.
"""

import ctypes
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes  # importable on all platforms; only *called* on Windows
from pathlib import Path
from typing import Optional

from graph_io import get_repo_root, log

# Flask app process (initialized in setup)
flask_process: Optional[subprocess.Popen] = None

# The port the live ``flask_process`` was actually bound to. Usually ``_graph_port()``,
# but stale-port recovery may relocate us to a free port, so we record the real one
# for the owner-file lifecycle and shutdown cleanup.
_owned_port: Optional[int] = None


def _agent_os_home() -> Path:
    """The machine-global ``~/.agent-os`` directory, honoring ``AGENT_OS_HOME``.

    A LOCAL copy of ``contracts.identity.machine_home()``: ``graph_server`` is
    imported by tests that put only ``mcp/`` on ``sys.path`` (e.g. test_probe_health),
    so it cannot import the top-level ``contracts`` package. Duplicating the tiny
    resolver — rather than adding that import — keeps those tests green while letting
    the registry/owner/log files relocate together for hermetic testing. Default is
    unchanged (``~/.agent-os``), so production behavior is identical.
    """
    override = os.environ.get("AGENT_OS_HOME")
    if override:
        return Path(os.path.expanduser(os.path.expandvars(override)))
    return Path.home() / ".agent-os"


def _repo_registry_path() -> Path:
    """Path of the shared active-repos registry (honors ``AGENT_OS_HOME``)."""
    return _agent_os_home() / "active_repos.json"


def _graph_port() -> int:
    """Port for the Flask graph UI. Override with AGENT_OS_GRAPH_PORT (default 5000)."""
    return int(os.getenv("AGENT_OS_GRAPH_PORT") or "5000")


def _graph_port_is_pinned() -> bool:
    """True iff the operator explicitly pinned the port via ``AGENT_OS_GRAPH_PORT``.

    When pinned we must NOT wander to a different port on a foreign conflict — the
    operator chose that port deliberately (e.g. a reverse-proxy expects it there).
    """
    return os.getenv("AGENT_OS_GRAPH_PORT") is not None


def _graph_log_path(port: int) -> Path:
    """Path of the log file the spawned graph server's stdout+stderr is redirected to.

    Lives under the same ``~/.agent-os/`` directory as the repo registry and is
    suffixed with the port so a custom AGENT_OS_GRAPH_PORT instance cannot clobber
    the default :5000 instance's log. Pure path builder — the spawn path is
    responsible for ensuring the parent directory exists (this is called from the
    read path too, which must not mutate the filesystem).
    """
    return _agent_os_home() / f"graph_server-{port}.log"


def _read_log_tail(port: int, max_bytes: int = 2000) -> str:
    """Return the last ``max_bytes`` of the graph server's log file as text.

    Used for post-exit crash diagnostics in place of ``communicate()`` now that
    the child's output goes to a file instead of a PIPE. Performs a bounded read
    (seek to the tail) so a large log is never loaded whole, and returns "" on any
    error (missing file, decode failure, etc.) so callers can log unconditionally.
    ``max_bytes`` is the single cap on how much tail is surfaced — callers log the
    result as-is rather than re-truncating.
    """
    try:
        log_path = _graph_log_path(port)
        with open(log_path, "rb") as fh:
            try:
                fh.seek(-max_bytes, os.SEEK_END)
            except OSError:
                # File shorter than max_bytes: read from the start.
                fh.seek(0)
            return fh.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _remove_empty_log(port: int) -> None:
    """Delete ``graph_server-<port>.log`` iff it exists and is EMPTY (best-effort).

    A 0-byte per-port log — left behind by a spawn that produced no output, or by a
    port we relocated away from — reads exactly like a crash signal and misdiagnosed
    this very bug for hours (card 2f94ecee, comments 158/160). Only an EMPTY file is
    ever removed, so a real crash log with diagnostics is never destroyed. All errors
    are swallowed: log hygiene must never affect startup.
    """
    try:
        path = _graph_log_path(port)
        if path.exists() and path.stat().st_size == 0:
            path.unlink()
    except OSError:
        pass


def _log_board_url(port: int, slug: str) -> None:
    """Emit the graph board URL for the ACTUAL bound ``port``.

    Single source of truth so every successful-start path surfaces the real port
    (not the configured base port, which differs after a stale-port relocation).
    """
    log(f"Graphs available at http://localhost:{port}/{slug}/ (code_graph, db_graph, task_cards)")


def _register_repo(repo_root: Path) -> str:
    """Write repo_root into the shared registry and return its URL slug.

    The slug is the repo directory name. If two repos share a name the
    parent directory is prepended (e.g. ``parent-reponame``) so slugs stay
    unique and human-readable.
    """
    slug = repo_root.name
    registry_path = _repo_registry_path()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        registry: dict = json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else {}
    except Exception:
        registry = {}

    existing = registry.get(slug)
    if existing and existing != str(repo_root):
        slug = f"{repo_root.parent.name}-{repo_root.name}"

    registry[slug] = str(repo_root)
    try:
        registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")
    except Exception:
        pass
    return slug


# --- Graph-server ownership registry (stale-port identity) -------------------
# When we spawn a graph server we record {pid, nonce, started_at} in a per-port
# "owner file" and pass the same nonce to the child (AGENT_OS_GRAPH_NONCE), which
# echoes it back on /health. This lets a LATER MCP-server process (e.g. after a
# Windows /reload-plugins where the old child orphaned) tell OUR wedged server
# apart from a genuinely foreign process holding the port: only a pid WE recorded
# spawning is ever a terminate candidate.

def _owner_file_path(port: int) -> Path:
    """Path of the per-port graph-server owner file (honors ``AGENT_OS_HOME``)."""
    return _agent_os_home() / f"graph_server-{port}.owner.json"


def _write_owner_file(port: int, pid: int, nonce: str) -> None:
    """Record {pid, nonce, started_at} for the server we just spawned on ``port``.

    Best-effort: a missing owner file only weakens stale-port identification, never
    breaks startup, so every error is swallowed.
    """
    try:
        path = _owner_file_path(port)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"pid": pid, "nonce": nonce, "started_at": time.time()}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _read_owner_file(port: int) -> Optional[dict]:
    """Return the recorded {pid, nonce, started_at} for ``port``, or None on any error."""
    try:
        return json.loads(_owner_file_path(port).read_text(encoding="utf-8"))
    except Exception:
        return None


def _clear_owner_file(port: int) -> None:
    """Remove the per-port owner file (best-effort)."""
    try:
        _owner_file_path(port).unlink()
    except OSError:
        pass


def _port_in_use(port: int) -> bool:
    """Return True if something is already listening on 127.0.0.1:<port>."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _probe_health_identity(port: int, timeout: float = 2.0) -> Optional[dict]:
    """Probe http://127.0.0.1:<port>/health and return the server's identity if it is ours.

    Returns ``{"pid": <int|None>, "nonce": <str|None>}`` only when the endpoint
    responds HTTP 200 with JSON that contains ``{"status": "ok", "repos": ...}`` —
    i.e. it IS the agent-os graph server. ``pid``/``nonce`` may be None for an older
    server that predates the identity fields; the non-None return still proves it is
    ours and reusable. Any other response — wrong status, missing keys, timeout,
    connection error — returns ``None`` (NOT ours / wedged / foreign / absent).

    Strictly time-bounded by ``timeout`` (connect + response). Uses stdlib
    ``http.client`` only; no third-party dependencies. Never raises.
    """
    import http.client

    conn = None
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        body = resp.read(4096).decode("utf-8", errors="replace")
        data = json.loads(body)
        # Must be our server: "status" == "ok" and "repos" key present.
        if data.get("status") == "ok" and "repos" in data:
            return {"pid": data.get("pid"), "nonce": data.get("nonce")}
        return None
    except Exception:
        return None
    finally:
        if conn is not None:
            conn.close()


def _probe_health(port: int, timeout: float = 2.0) -> bool:
    """True iff ``/health`` on ``port`` is the agent-os graph server. Thin bool wrapper
    over :func:`_probe_health_identity` (kept as a stable helper for existing callers
    and tests)."""
    return _probe_health_identity(port, timeout=timeout) is not None


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


# ---------------------------------------------------------------------------
# Windows kill-on-parent-exit (Job Object) — the os.name == "nt" analogue of the
# PR_SET_PDEATHSIG coupling above.
# ---------------------------------------------------------------------------
# Windows has neither preexec_fn nor pdeathsig, so a spawned child ORPHANS when the
# parent (MCP server) dies by SIGKILL / crash / a /reload-plugins teardown — exactly
# the field failure this fixes (the old graph server keeps :5000 across a reload).
# Instead we assign every spawned companion to a single process-wide Job Object
# created with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. The parent is the SOLE holder of
# the (non-inheritable) job handle, so when the parent dies by ANY means the OS
# closes that handle and terminates every process in the job. Windows 8+ supports
# nested jobs, so this works even if the MCP server is already inside a job
# (Win10+ target). All failures degrade gracefully to the pre-fix behavior.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9  # JOBOBJECTINFOCLASS enum value
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000  # right required to WaitForSingleObject on the handle
_WAIT_OBJECT_0 = 0x00000000  # WaitForSingleObject: the process object is signaled = EXITED
_WAIT_TIMEOUT = 0x00000102   # WaitForSingleObject: still running within the timeout

# Process-wide job handle, created once and held for the parent's lifetime. It is
# deliberately never closed while the server runs — its closure on process teardown
# is the very signal that reaps the children.
_job_handle = None
_job_unavailable = False  # set True after a failed create so we stop retrying
_kernel32 = None


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),   # LARGE_INTEGER
        ("PerJobUserTimeLimit", ctypes.c_int64),        # LARGE_INTEGER
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),     # SIZE_T
        ("MaximumWorkingSetSize", ctypes.c_size_t),     # SIZE_T
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),                  # ULONG_PTR
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _get_kernel32():
    """Lazily create + configure the kernel32 handle used for Job Object calls (Windows only)."""
    global _kernel32
    if _kernel32 is not None:
        return _kernel32
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32 = k32
    return _kernel32


def _ensure_job_object():
    """Create (once) the process-wide kill-on-close Job Object. Returns its handle or None.

    Windows only. Any failure logs once, disables further attempts, and returns None
    so the caller falls back to the pre-fix behavior (child not reaped on abrupt
    parent death) rather than crashing the spawn.
    """
    global _job_handle, _job_unavailable
    if _job_handle is not None:
        return _job_handle
    if _job_unavailable:
        return None
    try:
        k32 = _get_kernel32()
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(
            handle, _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info),
        )
        if not ok:
            err = ctypes.get_last_error()  # type: ignore[attr-defined]
            k32.CloseHandle(handle)
            raise ctypes.WinError(err)  # type: ignore[attr-defined]
        _job_handle = handle
        log("Windows Job Object created (kill-on-parent-exit) for companion processes.")
        return _job_handle
    except Exception as exc:
        _job_unavailable = True
        log(f"WARNING: Windows Job Object unavailable ({exc}); companions may orphan "
            f"if this process dies abruptly. Falling back to atexit cleanup.")
        return None


def _couple_child_to_parent(proc) -> None:
    """Best-effort: tie ``proc``'s lifetime to this process so it dies with the parent.

    Windows: assign the child to the process-wide kill-on-close Job Object.
    Non-Windows: NO-OP — Linux uses the PR_SET_PDEATHSIG preexec_fn attached in
    :func:`_spawn_graph_server` / ``sync_server._spawn_drain`` instead.

    Never raises: on any failure it logs and returns with the child still running —
    exactly the pre-fix behavior (the child simply isn't guaranteed to be reaped on
    an abrupt parent death). Call it right after ``subprocess.Popen``.
    """
    if os.name != "nt":
        return
    try:
        job = _ensure_job_object()
        if job is None:
            return
        k32 = _get_kernel32()
        h = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, proc.pid)
        if not h:
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        try:
            if not k32.AssignProcessToJobObject(job, h):
                raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
        finally:
            k32.CloseHandle(h)
        log(f"Companion PID {proc.pid} assigned to kill-on-parent-exit Job Object.")
    except Exception as exc:
        log(f"WARNING: could not assign companion PID {getattr(proc, 'pid', '?')} to "
            f"the Job Object ({exc}); it may orphan if this process dies abruptly.")


# ---------------------------------------------------------------------------
# Stale-port recovery helpers (bounded — must never hang startup).
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Cross-platform liveness probe for ``pid`` — does NOT signal/kill the target.

    POSIX: ``os.kill(pid, 0)`` (ProcessLookupError => dead; PermissionError => alive,
    owned by another user). Windows: ``os.kill(pid, 0)`` would route through
    TerminateProcess and KILL the target, so we must not use it — instead OpenProcess
    and then check the EXIT STATE. A valid handle alone is NOT enough: an exited
    process whose object is still referenced (e.g. its spawner still holds the handle)
    remains OpenProcess-able, so we must WaitForSingleObject(handle, 0) — a signaled
    (WAIT_OBJECT_0) object means the process has exited. On any ambiguity (wait fails)
    we conservatively report alive, since a false "dead" could terminate a live server
    while a false "alive" only makes stale-port recovery relocate instead of reclaim.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            k32 = _get_kernel32()
            handle = k32.OpenProcess(
                _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
            if not handle:
                return False  # process fully gone (no object at all)
            try:
                # 0 timeout => poll: WAIT_OBJECT_0 means signaled = exited.
                return k32.WaitForSingleObject(handle, 0) != _WAIT_OBJECT_0
            finally:
                k32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _find_free_port(base: int, span: int = 20) -> Optional[int]:
    """Return the first bindable localhost port in ``(base, base+span]``, or None.

    Bounded scan — never hangs. A port is "free" when a plain bind (no SO_REUSEADDR,
    so an in-use port is not falsely reported free on either platform) succeeds.
    """
    for candidate in range(base + 1, base + 1 + span):
        if candidate > 65535:
            break
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", candidate))
            return candidate
        except OSError:
            continue
    return None


def _owned_stale_pid(port: int) -> Optional[int]:
    """Return the pid we recorded spawning on ``port`` iff it is still ALIVE, else None.

    Consulted only AFTER /health has already failed for ``port``: a live pid we
    recorded owning the port but that no longer answers /health is our own wedged
    graph server (e.g. a Windows reload orphan) — the ONLY process we may safely
    terminate to reclaim the port. A dead/absent/foreign record => not a terminate
    candidate (we never terminate a process we did not record spawning).
    """
    owner = _read_owner_file(port)
    if not owner:
        return None
    pid = owner.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return None
    return pid if _pid_alive(pid) else None


def _terminate_pid_and_wait_free(pid: int, port: int, timeout: float = 3.0) -> bool:
    """Terminate ``pid`` and wait (bounded) for ``port`` to free. Return True if freed.

    ``os.kill(pid, SIGTERM)`` is cross-platform here: POSIX delivers SIGTERM; on
    Windows a non-CTRL signal routes through TerminateProcess. Strictly time-bounded
    so it can never hang startup; the owner file is cleared once the port is free.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError) as exc:
        log(f"Could not signal graph-server PID {pid}: {exc}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _port_in_use(port):
            _clear_owner_file(port)
            return True
        time.sleep(0.1)
    if not _port_in_use(port):
        _clear_owner_file(port)
        return True
    return False


def _resolve_port_conflict(port: int, slug: str) -> Optional[int]:
    """Decide what to do when ``port`` is already bound. Returns the port to spawn a
    fresh graph server on, or ``None`` if we should NOT spawn (a healthy server was
    reused, or the conflict could not be safely resolved).

    Strictly time-bounded — every branch returns immediately or performs a bounded
    probe/terminate/scan, so it NEVER hangs startup. Logging lives here so the caller
    stays a simple "spawn or don't" decision.
    """
    identity = _probe_health_identity(port)
    if identity is not None:
        # A working agent-os graph server already holds the port — ours-current,
        # ours-still-alive-from-a-previous-session/reload, or a peer MCP process's.
        # It serves ALL registered repos from the shared file registry, so reuse it
        # rather than fight it (this is the graceful path for a live Windows orphan).
        log(f"Graph server already running on port {port} "
            f"(health OK, pid={identity.get('pid')}); reusing it.")
        _log_board_url(port, slug)
        return None

    # Port is held but is NOT a healthy agent-os server: either OUR own previous
    # graph server wedged (holding the socket, not answering /health) or foreign.
    stale_pid = _owned_stale_pid(port)
    if stale_pid is not None:
        log(f"WARNING: port {port} is held by our previous graph server (PID {stale_pid}) "
            f"which is no longer answering /health — terminating it to reclaim the port.")
        if _terminate_pid_and_wait_free(stale_pid, port):
            log(f"Reclaimed port {port}; will respawn a fresh graph server there.")
            return port
        log(f"WARNING: could not free port {port} after terminating PID {stale_pid}.")
        # fall through to relocate / give up

    if _graph_port_is_pinned():
        log(f"WARNING: Port {port} is held by a foreign or unresponsive process and "
            f"AGENT_OS_GRAPH_PORT pins it explicitly — NOT relocating. The graph UI "
            f"will be unavailable until the port is freed. Card tools are unaffected.")
        return None

    alt = _find_free_port(port)
    if alt is None:
        log(f"WARNING: Port {port} is held by a foreign process and no free port was "
            f"found nearby; the graph UI will be unavailable. Card tools are unaffected.")
        return None
    log(f"Port {port} is held by a foreign process; relocating the graph UI to port {alt}.")
    return alt


def _spawn_graph_server(app_path: Path, repo_root: Path, port: int,
                        nonce: Optional[str] = None) -> "subprocess.Popen[bytes]":
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
    # Redirect the child's stdout+stderr to a file (truncated per spawn) instead
    # of a PIPE. The Werkzeug dev server logs every request to stderr; nobody
    # drains the pipe while the child is alive, so a PIPE fills its ~64KB kernel
    # buffer after sustained traffic and the child blocks mid-session. File writes
    # never block. Crash diagnostics are preserved by reading the tail of this log
    # post-exit (see _read_log_tail) instead of communicate().
    #
    # If the log file can't be opened (read-only home, full disk, ...), fall back
    # to DEVNULL so the graph server still STARTS — losing diagnostics is
    # acceptable, a silent no-start (which the old PIPE path never risked) is not.
    # Either way the child's output never goes to a PIPE, so it can't block.
    log_path = _graph_log_path(port)
    log_file = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "w", encoding="utf-8")
    except OSError as exc:
        log(f"WARNING: could not open graph server log {log_path} ({exc}); "
            f"discarding child output.")
    out = log_file if log_file is not None else subprocess.DEVNULL
    # The child echoes AGENT_OS_GRAPH_NONCE back on /health so a later MCP-server
    # process can recognize this exact instance (stale-port identity). PORT tells
    # app.py which port to bind.
    child_env = {**os.environ, "PORT": str(port)}
    if nonce is not None:
        child_env["AGENT_OS_GRAPH_NONCE"] = nonce
    try:
        proc = subprocess.Popen(
            [sys.executable, str(app_path)],
            cwd=repo_root,
            # stdin=DEVNULL is load-bearing on Windows: without it the child
            # INHERITS this process's stdin, which — when we run as the MCP
            # stdio server — is the Claude Code pipe the parent holds a
            # permanent blocking read on. Windows serializes synchronous I/O on
            # a shared pipe handle, so the child's interpreter startup (which
            # probes stdin inside Py_InitializeFromConfig) queues behind that
            # pending read and hangs before executing one line of Python: 0
            # CPU, empty log, board never binds. (M2 field diagnosis 2026-08-04,
            # proven by a py-spy native stack + minimal repro.) Harmless on POSIX.
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            env=child_env,
            preexec_fn=preexec,
        )
    finally:
        # Popen dup2's the fd into the child; the parent no longer needs its own
        # handle. Close it whether or not Popen succeeded.
        if log_file is not None:
            log_file.close()
    # Couple the child's lifetime to ours on Windows (Job Object). No-op on Linux,
    # where the PR_SET_PDEATHSIG preexec_fn above already does it. Best-effort.
    _couple_child_to_parent(proc)
    dest = log_path if log_file is not None else "DEVNULL"
    log(f"Graph server started (PID: {proc.pid}) at {app_path}; output -> {dest}")
    return proc


def start_graph_server():
    """Start the Flask graph server in a subprocess.

    Claude Code spawns a separate MCP server process for the main loop and for
    each subagent, so multiple instances would otherwise collide on the graph
    UI's single TCP port and leak orphan processes.

    Port-conflict logic (AC #1, bounded — see _resolve_port_conflict):
      If the port is already in use we probe /health. A healthy agent-os server is
      reused. OUR own wedged server (a Windows reload orphan, identified via the
      per-port owner file) is terminated and the port reclaimed. A foreign process
      makes us relocate to the next free port, unless AGENT_OS_GRAPH_PORT pins the
      port (then we respect the pin and don't spawn). This never hangs startup.

    Respawn logic (AC #2 + #3):
      If we previously spawned a child and it has since exited, surface its
      captured stderr and then respawn a fresh instance.

    Never raises: all failures are logged and swallowed so a companion problem can
    never propagate to ensure_agent_os() and take the card tools down.

    The port is configurable via AGENT_OS_GRAPH_PORT (default 5000).
    """
    global flask_process, _owned_port
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
        # output and clear the handle so we fall through to spawn a new one.
        # The child's stdout+stderr were redirected to a log file at spawn time,
        # so we read a bounded tail of that file for diagnostics rather than
        # draining a PIPE (see _read_log_tail).
        exit_code = flask_process.poll() if flask_process is not None else None
        if flask_process is not None and exit_code is not None:
            log(f"WARNING: Previously-spawned graph server (PID {flask_process.pid}) "
                f"exited with code {exit_code}; will respawn.")
            try:
                tail = _read_log_tail(port)
                if tail:
                    log(f"Graph server output before exit:\n{tail}")
            except Exception:
                pass
            flask_process = None

        # --- Already have a live owned child --------------------------------
        if flask_process is not None:
            # Our child is still running; nothing to do. Surface the ACTUAL bound
            # port (_owned_port) — a relocation may have moved us off the base port.
            log(f"Graph server already owned (PID {flask_process.pid}); reusing.")
            _log_board_url(_owned_port or port, slug)
            return

        # --- Port already in use? resolve it (reuse / reclaim-ours / relocate) --
        # _resolve_port_conflict is strictly bounded and does its own logging. It
        # returns the port to spawn on (either the reclaimed original or a relocated
        # free port), or None when an existing healthy server was reused or the
        # conflict could not be safely resolved (nothing for us to spawn).
        if _port_in_use(port):
            base_port = port
            resolved = _resolve_port_conflict(port, slug)
            if resolved is None:
                return
            if resolved != base_port:
                # Relocated off the base port — clear its stale (possibly empty) log
                # so a 0-byte graph_server-<base_port>.log for a port we're no longer
                # using can never again read as a crash signal (comment 160).
                _remove_empty_log(base_port)
            port = resolved

        # --- Spawn fresh -------------------------------------------------------
        # Tag this instance with a nonce (echoed on /health) and record ownership so
        # a future MCP-server process can tell our (possibly wedged) server apart
        # from a foreign one holding the port.
        nonce = uuid.uuid4().hex
        flask_process = _spawn_graph_server(app_path, repo_root, port, nonce=nonce)
        _owned_port = port
        _write_owner_file(port, flask_process.pid, nonce)
        _log_board_url(port, slug)

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
        # Process has exited — surface its output first. The child's stdout+stderr
        # were redirected to a log file at spawn time, so we read a bounded tail of
        # that file for diagnostics rather than draining a PIPE (see _read_log_tail).
        try:
            # Read the log for the port we ACTUALLY bound (_owned_port), not the
            # configured base port: stale-port recovery may have relocated this
            # instance to a different port, whose log is the one with the crash
            # output. _owned_port is set whenever we own a spawned child; the
            # _graph_port() fallback covers the theoretical unset case.
            tail = _read_log_tail(_owned_port or _graph_port())
            if tail:
                log(f"WARNING: Graph server crashed (exit {exit_code}). Recent output:\n{tail}")
            else:
                log(f"WARNING: Graph server crashed (exit {exit_code}).")
        except Exception:
            pass
        flask_process = None
        log("WARNING: Graph server process exited unexpectedly; attempting respawn.")
        # Respawn so the graph UI recovers without needing a full MCP restart.
        start_graph_server()


def shutdown_flask():
    """Terminate the Flask graph server subprocess and clear its ownership record."""
    global flask_process, _owned_port
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
    if _owned_port is not None:
        _clear_owner_file(_owned_port)
        _owned_port = None
