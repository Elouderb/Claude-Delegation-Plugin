"""Background outbox-drain companion lifecycle (Phase 1c, epic 78a6ac11).

Spawns ``python -m outbox.drain`` as a child of the MCP server, the way
``graph_server`` spawns the Flask graph UI — same orphan-reaping discipline
(PR_SET_PDEATHSIG on Linux, attached from the main thread; respawn-on-exit;
child output redirected to a log file, never a PIPE that could fill and block).

**Off by default.** The companion is spawned ONLY when ``AGENT_OS_SYNC=1`` is set,
so a stock install never phones home without explicit opt-in (proposal §5.5 /
local-first). With it unset, :func:`start_sync_worker` returns immediately and no
network process is ever created. The drain itself has its own kill-switch
(``AGENT_OS_SYNC_DISABLED=1``) and reads ``AGENT_OS_API_URL`` / ``AGENT_OS_API_KEY``
from an explicit env allowlist (see :func:`_child_env`).

Import resolution (the load-bearing spawn detail)
-------------------------------------------------
In a real plugin install the MCP server runs from the plugin root but serves a
DIFFERENT repo (cwd = that repo). ``python -m outbox.drain`` with that cwd cannot
import ``outbox`` / ``contracts`` — they live under the plugin root, which is not
on the child's ``sys.path``. So the child is spawned with ``PYTHONPATH`` prefixed
by the plugin root (:data:`_PLUGIN_ROOT`), exactly so ``-m outbox.drain`` resolves
from any cwd. Without this the feature silently no-ops everywhere except the dev
repo where the plugin *is* the cwd.

A best-effort per-repo pidfile guard avoids stacking several drains against one
outbox when Claude Code spawns multiple MCP-server processes (main loop + each
subagent). Idempotent central ingest makes a brief overlap harmless regardless.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from graph_io import log
from graph_server import _make_pdeathsig_preexec

# The spawned drain child we own (per MCP-server process) and the repo it covers,
# recorded so the health/respawn touchpoint can restart it under the same root.
sync_process: Optional["subprocess.Popen[bytes]"] = None
_sync_repo_root: Optional[Path] = None

_ENABLE_ENV = "AGENT_OS_SYNC"

# Plugin root = the directory holding outbox/, contracts/, services/, mcp/. This
# file is mcp/sync_server.py, so parent.parent is that root. Prepended to the
# child's PYTHONPATH so ``-m outbox.drain`` imports resolve from ANY cwd.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# Env the drain child is allowed to inherit. Deliberately does NOT include the DB
# connection strings (AGENT_OS_CENTRAL_DB_CONNECTION_STRING / DB_CONNECTION_STRING)
# — the drain never touches the DB directly, so it should never see them (blast-
# radius containment; security review comment 145 Low).
_CHILD_ENV_ALLOW = (
    "AGENT_OS_API_URL", "AGENT_OS_API_KEY",
    "AGENT_OS_SYNC", "AGENT_OS_SYNC_INTERVAL", "AGENT_OS_SYNC_BATCH",
    "AGENT_OS_SYNC_TIMEOUT", "AGENT_OS_SYNC_DISABLED",
    "AGENT_OS_HOME", "AGENT_OS_EVENTS_FSYNC", "AGENT_OS_EVENTS_DISABLED",
)
# Baseline OS vars any child needs to run at all (a subset is present per platform;
# only those actually set are forwarded). PYTHON* vars are forwarded by prefix.
_CHILD_ENV_SYSTEM = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TEMP", "TMP",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "SystemRoot", "SYSTEMROOT", "WINDIR", "USERPROFILE", "COMSPEC", "PATHEXT",
    "APPDATA", "LOCALAPPDATA", "NUMBER_OF_PROCESSORS",
)

# A pidfile older than this whose owner can't be confirmed as a live drain of ours
# is treated as stale (reclaimable), so a crashed process whose pid was recycled
# can never permanently block a respawn. Overridable for tests.
_PIDFILE_STALE_SECONDS = 12 * 3600


def sync_enabled() -> bool:
    """True iff the drain companion is opted in via ``AGENT_OS_SYNC=1``."""
    raw = os.environ.get(_ENABLE_ENV)
    return raw is not None and raw.strip().lower() in ("1", "true", "yes", "on")


def _repo_slug(repo_root: Path) -> str:
    """A filesystem-safe, per-repo-unique suffix for the log/pidfile names."""
    name = re.sub(r"[^A-Za-z0-9._-]", "-", repo_root.name) or "repo"
    digest = hashlib.sha1(str(repo_root).encode("utf-8")).hexdigest()[:8]
    return f"{name}-{digest}"


def _sync_log_path(repo_root: Path) -> Path:
    """Per-repo log file for the spawned drain's stdout+stderr.

    Per-repo (suffixed like the graph server's per-port log) AND opened in APPEND
    mode, so a second repo's spawn can't wipe this repo's log and a respawn/crash
    loop leaves a visible trail instead of truncating away the evidence.
    """
    return Path.home() / ".agent-os" / f"sync_worker-{_repo_slug(repo_root)}.log"


def _read_log_tail(repo_root: Path, max_bytes: int = 2000) -> str:
    """Return the last ``max_bytes`` of this repo's drain log, or "" on any error."""
    try:
        with open(_sync_log_path(repo_root), "rb") as fh:
            try:
                fh.seek(-max_bytes, os.SEEK_END)
            except OSError:
                fh.seek(0)
            return fh.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def _child_env() -> Dict[str, str]:
    """Build the drain child's environment: an explicit allowlist + PYTHONPATH.

    Only the vars the drain actually needs are forwarded (the DB connection strings
    are withheld). The plugin root is prepended to PYTHONPATH so ``-m outbox.drain``
    resolves ``outbox``/``contracts`` regardless of the child's cwd.
    """
    env: Dict[str, str] = {}
    for key in _CHILD_ENV_SYSTEM:
        if key in os.environ:
            env[key] = os.environ[key]
    for key, value in os.environ.items():
        if key.startswith("PYTHON"):
            env[key] = value
    for key in _CHILD_ENV_ALLOW:
        if key in os.environ:
            env[key] = os.environ[key]
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(_PLUGIN_ROOT) + (os.pathsep + existing if existing else "")
    )
    return env


def _pidfile_path(repo_root: Path) -> Path:
    return repo_root / ".agent-os" / "sync" / "drain.pid"


def _read_pid(pidfile: Path) -> Optional[int]:
    try:
        return int(pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pidfile_is_stale(pidfile: Path) -> bool:
    try:
        age = time.time() - pidfile.stat().st_mtime
    except OSError:
        return True
    return age > _PIDFILE_STALE_SECONDS


def _another_drain_running(repo_root: Path) -> bool:
    """True iff a live drain we DON'T own already covers this repo.

    Conservative and self-healing:
      * our own recorded child -> not "another" (the respawn check handles it);
      * a stale pidfile (older than the reclaim window) -> ignored, so a recycled
        pid can't block a respawn forever;
      * a dead pid (ProcessLookupError) -> not running;
      * a pid we can't signal (PermissionError -> owned by another user) -> NOT our
        drain (ours run as us), so we don't defer to it;
      * an alive, same-user pid -> a real peer drain: defer.
    """
    pidfile = _pidfile_path(repo_root)
    pid = _read_pid(pidfile)
    if pid is None or pid <= 0:
        return False
    if sync_process is not None and pid == sync_process.pid:
        return False
    if _pidfile_is_stale(pidfile):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except (OSError, AttributeError):  # pragma: no cover - platform-dependent
        return False


def _write_pidfile(repo_root: Path, pid: int) -> None:
    """Write the pidfile, refusing to follow a pre-planted symlink where possible.

    Best-effort: a missing pidfile only weakens the dedupe, never safety. Uses
    ``O_NOFOLLOW`` (where available) so a symlink at the path is not followed
    (hardening for the documented future multi-user/LAN posture).
    """
    pidfile = _pidfile_path(repo_root)
    try:
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(pidfile), flags, 0o644)
        try:
            os.write(fd, str(pid).encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        try:  # fallback for platforms/filesystems that reject the flags
            pidfile.write_text(str(pid), encoding="utf-8")
        except OSError:
            pass


def _remove_pidfile(repo_root: Path) -> None:
    try:
        _pidfile_path(repo_root).unlink()
    except OSError:
        pass


def _spawn_drain(repo_root: Path) -> Optional["subprocess.Popen[bytes]"]:
    """Launch ``python -m outbox.drain`` for ``repo_root`` and return the handle.

    Mirrors ``graph_server._spawn_graph_server``: PR_SET_PDEATHSIG is attached only
    from the main thread (it keys off the spawning thread's lifetime); the child's
    output is appended to a per-repo log file (falling back to DEVNULL) so it can
    never block on a full PIPE. The child env is the withholding allowlist plus the
    plugin-root PYTHONPATH (see :func:`_child_env`).
    """
    parent_pid = os.getpid()
    on_main_thread = threading.current_thread() is threading.main_thread()
    use_pdeathsig = sys.platform == "linux" and on_main_thread
    preexec = _make_pdeathsig_preexec(parent_pid) if use_pdeathsig else None

    log_path = _sync_log_path(repo_root)
    log_file = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "a", encoding="utf-8")  # append: preserve history
    except OSError as exc:
        log(f"WARNING: could not open sync worker log {log_path} ({exc}); "
            f"discarding child output.")
    out = log_file if log_file is not None else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "outbox.drain", "--repo-root", str(repo_root)],
            cwd=str(repo_root),
            stdout=out,
            stderr=subprocess.STDOUT,
            env=_child_env(),
            preexec_fn=preexec,
        )
    except Exception as exc:
        log(f"ERROR spawning sync worker: {exc}")
        return None
    finally:
        if log_file is not None:
            log_file.close()
    dest = log_path if log_file is not None else "DEVNULL"
    log(f"Sync worker started (PID: {proc.pid}); output -> {dest}")
    return proc


def start_sync_worker(repo_root: Path) -> None:
    """Start the drain companion for ``repo_root`` if opted in and not already up.

    No-op (and never raises) unless ``AGENT_OS_SYNC=1``. Safe to call repeatedly —
    it reuses a live owned child, respawns a dead one, and defers to another
    process's live drain via the pidfile guard.
    """
    global sync_process, _sync_repo_root
    if not sync_enabled():
        return
    try:
        repo_root = Path(repo_root)

        # Respawn check: if our owned child has exited, surface its log tail and
        # clear it so we respawn below.
        if sync_process is not None and sync_process.poll() is not None:
            tail = _read_log_tail(_sync_repo_root or repo_root)
            log(f"WARNING: sync worker (PID {sync_process.pid}) exited with code "
                f"{sync_process.returncode}; will respawn."
                + (f" Recent output:\n{tail}" if tail else ""))
            sync_process = None

        # Already have a live owned child — nothing to do.
        if sync_process is not None:
            log(f"Sync worker already owned (PID {sync_process.pid}); reusing.")
            return

        # Another MCP-server process already runs a drain for this repo.
        if _another_drain_running(repo_root):
            log("Sync worker already running for this repo (pidfile); not spawning another.")
            return

        proc = _spawn_drain(repo_root)
        if proc is not None:
            sync_process = proc
            _sync_repo_root = repo_root
            _write_pidfile(repo_root, proc.pid)
    except Exception as exc:
        log(f"ERROR starting sync worker: {exc}")


def check_sync_worker_health() -> None:
    """Respawn the drain if our owned child has crashed. Cheap poll/respawn touchpoint.

    Wired into the existing graph-access health path (``mcp/graph_io``), a
    low-frequency call site. Cost per call is one non-blocking ``Popen.poll()``; the
    respawn body runs only when the child has actually exited. A child that exited 0
    (deliberate stop: disabled, fatal-config, or interrupt — the drain's own
    ``run_forever`` never returns 0 while healthy) is NOT respawned, so a
    misconfiguration doesn't become a crash loop; a nonzero (crash) exit is
    respawned with its log tail surfaced. No-op if sync is disabled or unspawned.
    """
    global sync_process, _sync_repo_root
    if not sync_enabled() or sync_process is None or _sync_repo_root is None:
        return
    if sync_process.poll() is None:
        return  # still alive
    repo_root = _sync_repo_root
    code = sync_process.returncode
    tail = _read_log_tail(repo_root)
    pid = sync_process.pid
    sync_process = None
    if code == 0:
        log(f"Sync worker (PID {pid}) exited cleanly (code 0); not respawning."
            + (f" Recent output:\n{tail}" if tail else ""))
        _remove_pidfile(repo_root)
        return
    log(f"WARNING: sync worker (PID {pid}) crashed (exit {code}); respawning."
        + (f" Recent output:\n{tail}" if tail else ""))
    start_sync_worker(repo_root)


def shutdown_sync_worker() -> None:
    """Terminate the drain companion subprocess and clean up its pidfile (best-effort)."""
    global sync_process, _sync_repo_root
    repo_root = _sync_repo_root
    if sync_process is None:
        if repo_root is not None:
            _remove_pidfile(repo_root)
        return
    try:
        sync_process.terminate()
        sync_process.wait(timeout=5)
        log("Sync worker terminated")
    except subprocess.TimeoutExpired:
        sync_process.kill()
        log("Sync worker killed")
    except Exception as exc:
        log(f"ERROR shutting down sync worker: {exc}")
    finally:
        sync_process = None
        if repo_root is not None:
            _remove_pidfile(repo_root)
        _sync_repo_root = None


__all__ = [
    "sync_enabled",
    "start_sync_worker",
    "check_sync_worker_health",
    "shutdown_sync_worker",
]
