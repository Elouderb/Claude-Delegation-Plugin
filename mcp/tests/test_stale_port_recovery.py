"""Stale-port recovery unit tests (card 2f94ecee, scope item 3).

Covers the bounded ours-vs-foreign port-conflict decision and its helpers in
``graph_server``:

* ``_probe_health_identity`` returns {pid, nonce} for a real agent-os /health and
  None for a foreign 200 / non-200 / closed port (and the bool ``_probe_health``
  wrapper stays consistent — the frozen test_probe_health contract);
* ``_resolve_port_conflict`` decision matrix: reuse a healthy server; reclaim OUR
  wedged server (terminate + rebind); relocate off a foreign default port; RESPECT
  an operator-pinned AGENT_OS_GRAPH_PORT (never wander); fall through to relocate
  when a reclaim attempt fails to free the port;
* ``_owned_stale_pid`` only nominates a live pid we recorded spawning;
* ``_pid_alive`` and ``_find_free_port`` behave and stay bounded;
* the real app ``/health`` echoes ``pid`` + ``nonce`` (the identity contract the
  parent relies on).

Everything is stdlib + fast: the decision-matrix tests mock the network probe so
they never wait on a real timeout; the identity-probe tests use tiny in-process
HTTP servers on ephemeral ports.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

_MCP_DIR = Path(__file__).resolve().parents[1]
_DB_TOOLS_DIR = _MCP_DIR / "db_tools"
for _p in (str(_MCP_DIR), str(_DB_TOOLS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import graph_server  # noqa: E402


# ---------------------------------------------------------------------------
# Fake /health servers (for the real identity-probe tests)
# ---------------------------------------------------------------------------

def _start_fake_server(handler_cls) -> tuple[HTTPServer, int]:
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, port


class _HealthyIdentityHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({
            "status": "ok", "repos": ["r"], "pid": 4321, "nonce": "nonce-xyz",
        }).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # match BaseHTTPRequestHandler signature
        pass


class _ForeignHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", "version": "9"}).encode()  # no "repos"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # match BaseHTTPRequestHandler signature
        pass


class TestProbeHealthIdentity(unittest.TestCase):
    def test_healthy_returns_pid_and_nonce(self):
        srv, port = _start_fake_server(_HealthyIdentityHandler)
        try:
            identity = graph_server._probe_health_identity(port)
            self.assertIsNotNone(identity)
            assert identity is not None  # narrow for type-checkers
            self.assertEqual(identity["pid"], 4321)
            self.assertEqual(identity["nonce"], "nonce-xyz")
            # bool wrapper stays consistent (frozen test_probe_health contract)
            self.assertTrue(graph_server._probe_health(port))
        finally:
            srv.shutdown()

    def test_foreign_returns_none(self):
        srv, port = _start_fake_server(_ForeignHandler)
        try:
            self.assertIsNone(graph_server._probe_health_identity(port))
            self.assertFalse(graph_server._probe_health(port))
        finally:
            srv.shutdown()

    def test_closed_port_returns_none(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            dead = s.getsockname()[1]
        self.assertIsNone(graph_server._probe_health_identity(dead, timeout=0.5))


# ---------------------------------------------------------------------------
# _resolve_port_conflict decision matrix (network probe mocked for determinism)
# ---------------------------------------------------------------------------

class TestResolvePortConflict(unittest.TestCase):
    def setUp(self):
        self._prev_pin = os.environ.get("AGENT_OS_GRAPH_PORT")
        os.environ.pop("AGENT_OS_GRAPH_PORT", None)

    def tearDown(self):
        if self._prev_pin is None:
            os.environ.pop("AGENT_OS_GRAPH_PORT", None)
        else:
            os.environ["AGENT_OS_GRAPH_PORT"] = self._prev_pin

    def test_healthy_server_is_reused_not_touched(self):
        with mock.patch.object(graph_server, "_probe_health_identity",
                               return_value={"pid": 111, "nonce": "n"}), \
             mock.patch.object(graph_server, "_terminate_pid_and_wait_free") as term, \
             mock.patch.object(graph_server, "_find_free_port") as findfree:
            result = graph_server._resolve_port_conflict(5000, "slug")
        self.assertIsNone(result)          # reuse -> do not spawn
        term.assert_not_called()           # never terminate a healthy server
        findfree.assert_not_called()       # never relocate off a healthy server

    def test_our_wedged_server_is_reclaimed(self):
        with mock.patch.object(graph_server, "_probe_health_identity", return_value=None), \
             mock.patch.object(graph_server, "_owned_stale_pid", return_value=4242), \
             mock.patch.object(graph_server, "_terminate_pid_and_wait_free",
                               return_value=True) as term:
            result = graph_server._resolve_port_conflict(5000, "slug")
        self.assertEqual(result, 5000)     # reclaimed -> respawn on the SAME port
        term.assert_called_once_with(4242, 5000)

    def test_reclaim_failure_falls_through_to_relocate(self):
        with mock.patch.object(graph_server, "_probe_health_identity", return_value=None), \
             mock.patch.object(graph_server, "_owned_stale_pid", return_value=4242), \
             mock.patch.object(graph_server, "_terminate_pid_and_wait_free",
                               return_value=False), \
             mock.patch.object(graph_server, "_find_free_port", return_value=5007):
            result = graph_server._resolve_port_conflict(5000, "slug")
        self.assertEqual(result, 5007)     # could not free -> relocate

    def test_foreign_default_port_relocates(self):
        with mock.patch.object(graph_server, "_probe_health_identity", return_value=None), \
             mock.patch.object(graph_server, "_owned_stale_pid", return_value=None), \
             mock.patch.object(graph_server, "_find_free_port", return_value=5009):
            result = graph_server._resolve_port_conflict(5000, "slug")
        self.assertEqual(result, 5009)

    def test_foreign_pinned_port_is_respected_not_relocated(self):
        os.environ["AGENT_OS_GRAPH_PORT"] = "5000"  # operator pinned it
        with mock.patch.object(graph_server, "_probe_health_identity", return_value=None), \
             mock.patch.object(graph_server, "_owned_stale_pid", return_value=None), \
             mock.patch.object(graph_server, "_find_free_port") as findfree:
            result = graph_server._resolve_port_conflict(5000, "slug")
        self.assertIsNone(result)          # respect the pin -> do not spawn
        findfree.assert_not_called()       # and never wander to another port


# ---------------------------------------------------------------------------
# Ownership + liveness helpers
# ---------------------------------------------------------------------------

class TestOwnedStalePid(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="agent_os_owner_")
        self._prev_home = os.environ.get("AGENT_OS_HOME")
        os.environ["AGENT_OS_HOME"] = self._tmp.name

    def tearDown(self):
        if self._prev_home is None:
            os.environ.pop("AGENT_OS_HOME", None)
        else:
            os.environ["AGENT_OS_HOME"] = self._prev_home
        self._tmp.cleanup()

    def test_live_recorded_pid_is_nominated(self):
        graph_server._write_owner_file(5000, os.getpid(), "nonce")
        with mock.patch.object(graph_server, "_pid_alive", return_value=True):
            self.assertEqual(graph_server._owned_stale_pid(5000), os.getpid())

    def test_dead_recorded_pid_is_not_nominated(self):
        graph_server._write_owner_file(5000, 4242, "nonce")
        with mock.patch.object(graph_server, "_pid_alive", return_value=False):
            self.assertIsNone(graph_server._owned_stale_pid(5000))

    def test_absent_owner_file_returns_none(self):
        self.assertIsNone(graph_server._owned_stale_pid(6123))

    def test_owner_file_roundtrip_and_clear(self):
        graph_server._write_owner_file(5000, 777, "abc")
        rec = graph_server._read_owner_file(5000)
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertEqual(rec["pid"], 777)
        self.assertEqual(rec["nonce"], "abc")
        self.assertIn("started_at", rec)
        graph_server._clear_owner_file(5000)
        self.assertIsNone(graph_server._read_owner_file(5000))


class TestPidAliveAndFreePort(unittest.TestCase):
    def test_pid_alive_true_for_self(self):
        self.assertTrue(graph_server._pid_alive(os.getpid()))

    def test_pid_alive_false_for_nonpositive(self):
        self.assertFalse(graph_server._pid_alive(0))
        self.assertFalse(graph_server._pid_alive(-1))

    def test_pid_alive_false_for_exited_child(self):
        # Spawn and reap a child; its pid is dead immediately afterward.
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=30)
        self.assertFalse(graph_server._pid_alive(proc.pid))

    def test_find_free_port_returns_bindable_port_in_range(self):
        # Take an ephemeral port as the base, then scan above it.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            base = s.getsockname()[1]
        alt = graph_server._find_free_port(base, span=50)
        self.assertIsNotNone(alt)
        assert alt is not None
        self.assertTrue(base < alt <= base + 50)
        # It must actually be bindable.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
            s2.bind(("127.0.0.1", alt))  # would raise if not free

    def test_find_free_port_none_when_out_of_range(self):
        self.assertIsNone(graph_server._find_free_port(65535, span=10))


# ---------------------------------------------------------------------------
# The real app /health identity contract (pid + nonce)
# ---------------------------------------------------------------------------

class TestAppHealthIdentity(unittest.TestCase):
    def test_health_echoes_pid_and_nonce(self):
        prev = os.environ.get("AGENT_OS_GRAPH_NONCE")
        os.environ["AGENT_OS_GRAPH_NONCE"] = "health-nonce-123"
        try:
            import app as flask_app_module  # type: ignore[import]  # db_tools on sys.path at runtime
            client = flask_app_module.app.test_client()
            resp = client.get("/health")
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data["status"], "ok")
            self.assertIn("repos", data)
            self.assertEqual(data["pid"], os.getpid())
            self.assertEqual(data["nonce"], "health-nonce-123")
        finally:
            if prev is None:
                os.environ.pop("AGENT_OS_GRAPH_NONCE", None)
            else:
                os.environ["AGENT_OS_GRAPH_NONCE"] = prev


# ---------------------------------------------------------------------------
# Per-port log hygiene: _remove_empty_log (fold-in comment 160, item 3)
# ---------------------------------------------------------------------------

class TestRemoveEmptyLog(unittest.TestCase):
    """A stale 0-byte per-port log must be removable without ever destroying a real
    (non-empty) crash log — an empty log is exactly what misdiagnosed this bug."""

    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="agent_os_emptylog_")
        self._prev_home = os.environ.get("AGENT_OS_HOME")
        os.environ["AGENT_OS_HOME"] = self._tmp.name

    def tearDown(self):
        if self._prev_home is None:
            os.environ.pop("AGENT_OS_HOME", None)
        else:
            os.environ["AGENT_OS_HOME"] = self._prev_home
        self._tmp.cleanup()

    def test_empty_log_is_removed(self):
        p = graph_server._graph_log_path(5000)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
        self.assertTrue(p.exists())
        graph_server._remove_empty_log(5000)
        self.assertFalse(p.exists())

    def test_nonempty_log_is_kept(self):
        p = graph_server._graph_log_path(5000)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("Traceback: boom", encoding="utf-8")
        graph_server._remove_empty_log(5000)
        self.assertTrue(p.exists())  # a real crash log is never destroyed

    def test_missing_log_is_noop(self):
        graph_server._remove_empty_log(6543)  # must not raise


# ---------------------------------------------------------------------------
# start_graph_server / _check_graph_server_health after a relocation
# (MAJOR-2 comment 166 + fold-in comment 160 item 3). These mutate module
# globals, so setUp/tearDown snapshot and restore them.
# ---------------------------------------------------------------------------

class TestRelocationLifecycle(unittest.TestCase):
    def setUp(self):
        self._prev_proc = graph_server.flask_process
        self._prev_owned = graph_server._owned_port
        self._prev_pin = os.environ.get("AGENT_OS_GRAPH_PORT")
        os.environ.pop("AGENT_OS_GRAPH_PORT", None)  # base port -> default 5000

    def tearDown(self):
        graph_server.flask_process = self._prev_proc
        graph_server._owned_port = self._prev_owned
        if self._prev_pin is None:
            os.environ.pop("AGENT_OS_GRAPH_PORT", None)
        else:
            os.environ["AGENT_OS_GRAPH_PORT"] = self._prev_pin

    def test_crash_reads_relocated_owned_port_log_not_base(self):
        # A crashed instance that was relocated to 5007 must have its 5007 log read,
        # not the configured base port's (5000) — the wrong, empty file.
        graph_server.flask_process = SimpleNamespace(poll=lambda: 1, pid=9999)
        graph_server._owned_port = 5007  # relocated, != base 5000
        seen: list = []
        with mock.patch.object(graph_server, "_read_log_tail",
                               side_effect=lambda port, *a, **k: seen.append(port) or ""), \
                mock.patch.object(graph_server, "start_graph_server"):
            graph_server._check_graph_server_health()
        self.assertEqual(seen, [5007])
        self.assertNotIn(graph_server._graph_port(), seen)  # base 5000 never read

    def test_relocation_cleans_abandoned_base_port_empty_log(self):
        # base 5000 held by a foreign process -> relocate to 5007; the abandoned
        # base-port (possibly empty) log must be cleaned so it can't read as a crash.
        graph_server.flask_process = None
        fake_proc = SimpleNamespace(pid=1234, poll=lambda: None)
        with mock.patch.object(graph_server, "_register_repo", return_value="slug"), \
                mock.patch.object(graph_server, "_graph_port", return_value=5000), \
                mock.patch.object(graph_server, "_port_in_use", return_value=True), \
                mock.patch.object(graph_server, "_resolve_port_conflict", return_value=5007), \
                mock.patch.object(graph_server, "_spawn_graph_server", return_value=fake_proc), \
                mock.patch.object(graph_server, "_write_owner_file"), \
                mock.patch.object(graph_server, "_log_board_url"), \
                mock.patch.object(graph_server, "_remove_empty_log") as rm:
            graph_server.start_graph_server()
        rm.assert_called_once_with(5000)
        self.assertEqual(graph_server._owned_port, 5007)  # tracked the real bound port

    def test_no_relocation_does_not_clean_base_log(self):
        # base 5000 free -> spawn on 5000; no relocation, so no log cleanup.
        graph_server.flask_process = None
        fake_proc = SimpleNamespace(pid=1234, poll=lambda: None)
        with mock.patch.object(graph_server, "_register_repo", return_value="slug"), \
                mock.patch.object(graph_server, "_graph_port", return_value=5000), \
                mock.patch.object(graph_server, "_port_in_use", return_value=False), \
                mock.patch.object(graph_server, "_spawn_graph_server", return_value=fake_proc), \
                mock.patch.object(graph_server, "_write_owner_file"), \
                mock.patch.object(graph_server, "_log_board_url"), \
                mock.patch.object(graph_server, "_remove_empty_log") as rm:
            graph_server.start_graph_server()
        rm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
