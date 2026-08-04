"""Companion-lifecycle tests for mcp/sync_server.py (card 8f84948b review pass).

Covers the load-bearing spawn fix and the pidfile hardening:

* the drain child's env carries the plugin-root PYTHONPATH and WITHHOLDS the DB
  connection strings (env allowlist);
* ``python -m outbox.drain`` actually imports and runs from a cwd that is NOT the
  plugin root (the real plugin-install case) — no ModuleNotFoundError;
* the pidfile guard treats a PermissionError / stale pidfile as "not our drain"
  (so a recycled pid can't permanently block a respawn) and a live same-user pid
  as running;
* the pidfile is removed on shutdown.

Stdlib + a real subprocess only; no network beyond a guaranteed-refused port.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

_MCP_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_MCP_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sync_server  # noqa: E402


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _FakeProc:
    def __init__(self, pid=1234567):
        self.pid = pid
        self.returncode = 0
        self.terminated = False

    def poll(self):
        return 0  # already exited

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


class TestChildEnv(unittest.TestCase):
    def test_plugin_root_on_pythonpath_and_db_strings_withheld(self):
        with mock.patch.dict(os.environ, {
            "AGENT_OS_API_URL": "http://127.0.0.1:8765",
            "AGENT_OS_API_KEY": "secret",
            "AGENT_OS_CENTRAL_DB_CONNECTION_STRING": "DRIVER=...;PWD=hunter2;",
            "DB_CONNECTION_STRING": "DRIVER=...;PWD=hunter2;",
        }, clear=False):
            env = sync_server._child_env()
        self.assertIn(str(sync_server._PLUGIN_ROOT), env["PYTHONPATH"].split(os.pathsep))
        self.assertEqual(env.get("AGENT_OS_API_URL"), "http://127.0.0.1:8765")
        self.assertEqual(env.get("AGENT_OS_API_KEY"), "secret")
        # The DB connection strings are NEVER forwarded to the drain child.
        self.assertNotIn("AGENT_OS_CENTRAL_DB_CONNECTION_STRING", env)
        self.assertNotIn("DB_CONNECTION_STRING", env)


class TestSpawnResolvesImportsFromAnyCwd(unittest.TestCase):
    def test_drain_runs_from_non_plugin_cwd(self):
        # The real plugin-install case: the child runs with cwd = the served repo,
        # NOT the plugin root. With the plugin-root PYTHONPATH the -m import must
        # resolve; the drain then fails to REGISTER (unreachable API) rather than
        # dying with ModuleNotFoundError.
        with TemporaryDirectory(prefix="agent_os_") as tmp:
            base = Path(tmp)
            cwd = base / "elsewhere"       # a dir that is NOT the plugin root
            repo = base / "repo"
            home = base / "home"
            for d in (cwd, repo, home):
                d.mkdir()
            env = sync_server._child_env()
            env["AGENT_OS_HOME"] = str(home)
            env["AGENT_OS_API_URL"] = f"http://127.0.0.1:{_free_port()}"  # refused
            env["AGENT_OS_API_KEY"] = "k"
            env["AGENT_OS_SYNC_INTERVAL"] = "1"
            proc = subprocess.run(
                [sys.executable, "-m", "outbox.drain", "--once", "--repo-root", str(repo)],
                cwd=str(cwd), env=env, capture_output=True, text=True,
                encoding="utf-8", timeout=60,
            )
        stderr = proc.stderr
        self.assertNotIn("ModuleNotFoundError", stderr, msg=stderr)
        self.assertNotIn("No module named", stderr, msg=stderr)
        # Proof it imported AND ran run_once: it reached the register-failure path.
        self.assertIn("could not register", stderr, msg=stderr)
        self.assertEqual(proc.returncode, 1)  # could-not-register exit code


class TestPidfileGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="agent_os_")
        self.repo = Path(self._tmp.name)
        (self.repo / ".agent-os" / "sync").mkdir(parents=True)
        self._prev_proc = sync_server.sync_process
        self._prev_root = sync_server._sync_repo_root
        sync_server.sync_process = None
        sync_server._sync_repo_root = None

    def tearDown(self):
        sync_server.sync_process = self._prev_proc
        sync_server._sync_repo_root = self._prev_root
        self._tmp.cleanup()

    def _write_pid(self, pid):
        sync_server._write_pidfile(self.repo, pid)

    def test_permission_error_pid_is_not_our_drain(self):
        self._write_pid(999999)
        with mock.patch("os.kill", side_effect=PermissionError):
            self.assertFalse(sync_server._another_drain_running(self.repo))

    def test_live_same_user_pid_is_running(self):
        self._write_pid(os.getpid())  # a real, alive, same-user pid
        self.assertTrue(sync_server._another_drain_running(self.repo))

    def test_dead_pid_is_not_running(self):
        self._write_pid(os.getpid())
        with mock.patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(sync_server._another_drain_running(self.repo))

    def test_stale_pidfile_is_reclaimable(self):
        self._write_pid(os.getpid())  # alive, but the pidfile is old
        pidfile = sync_server._pidfile_path(self.repo)
        old = time.time() - (sync_server._PIDFILE_STALE_SECONDS + 60)
        os.utime(pidfile, (old, old))
        self.assertFalse(sync_server._another_drain_running(self.repo))

    def test_pidfile_removed_on_shutdown(self):
        self._write_pid(4242)
        self.assertTrue(sync_server._pidfile_path(self.repo).exists())
        sync_server.sync_process = _FakeProc()
        sync_server._sync_repo_root = self.repo
        sync_server.shutdown_sync_worker()
        self.assertFalse(sync_server._pidfile_path(self.repo).exists())
        self.assertIsNone(sync_server.sync_process)


if __name__ == "__main__":
    unittest.main()
