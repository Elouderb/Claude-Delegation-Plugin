#!/usr/bin/env python3
"""
Unit tests for the _probe_health helper in mcp/server.py.

Three fake listeners are exercised:
  1. Healthy agent-os server  => _probe_health returns True
  2. Unhealthy / non-200      => _probe_health returns False
  3. Foreign 200 with wrong JSON (no "repos" key) => _probe_health returns False

Each fake is a minimal HTTP server started on an ephemeral port in a background
thread.  Uses stdlib only (http.server, threading, json).
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# _probe_health lives in graph_server after the server.py split. graph_server
# imports only from graph_io (no MCP/FastMCP), so this needs no patching.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from graph_server import _probe_health


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _start_fake_server(handler_cls) -> tuple[HTTPServer, int]:
    """Bind to an OS-assigned port, start serving in a daemon thread, return (server, port)."""
    srv = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, port


# ---------------------------------------------------------------------------
# Fake handlers
# ---------------------------------------------------------------------------

class _HealthyHandler(BaseHTTPRequestHandler):
    """Returns HTTP 200 + the expected agent-os JSON body."""

    def do_GET(self):
        body = json.dumps({"status": "ok", "repos": ["my-repo"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass  # silence request logs during tests


class _Non200Handler(BaseHTTPRequestHandler):
    """Returns HTTP 503 — simulates an unhealthy / starting-up server."""

    def do_GET(self):
        body = b"Service Unavailable"
        self.send_response(503)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class _ForeignHandler(BaseHTTPRequestHandler):
    """Returns HTTP 200 but with JSON that lacks the 'repos' key — a foreign server."""

    def do_GET(self):
        body = json.dumps({"status": "ok", "version": "1.2.3"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_healthy_agent_os_server():
    """_probe_health must return True when /health returns the expected JSON."""
    srv, port = _start_fake_server(_HealthyHandler)
    try:
        result = _probe_health(port)
        assert result is True, (
            f"Expected True for healthy agent-os server on port {port}, got {result}"
        )
        print(f"  PASS: healthy server on port {port} -> True")
    finally:
        srv.shutdown()


def test_non_200_listener():
    """_probe_health must return False when the HTTP status is not 200."""
    srv, port = _start_fake_server(_Non200Handler)
    try:
        result = _probe_health(port)
        assert result is False, (
            f"Expected False for non-200 response on port {port}, got {result}"
        )
        print(f"  PASS: non-200 server on port {port} -> False")
    finally:
        srv.shutdown()


def test_foreign_200_listener():
    """_probe_health must return False when the JSON lacks the 'repos' key."""
    srv, port = _start_fake_server(_ForeignHandler)
    try:
        result = _probe_health(port)
        assert result is False, (
            f"Expected False for foreign-200 server on port {port}, got {result}"
        )
        print(f"  PASS: foreign-200 server on port {port} -> False")
    finally:
        srv.shutdown()


def test_closed_port():
    """_probe_health must return False when nothing is listening (connection refused)."""
    # Grab a port then immediately free it so nothing listens.
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    # Port is now closed.
    result = _probe_health(dead_port, timeout=0.5)
    assert result is False, (
        f"Expected False for closed port {dead_port}, got {result}"
    )
    print(f"  PASS: closed port {dead_port} -> False")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Testing _probe_health ...\n")
    failures = []
    for fn in [
        test_healthy_agent_os_server,
        test_non_200_listener,
        test_foreign_200_listener,
        test_closed_port,
    ]:
        try:
            fn()
        except AssertionError as exc:
            print(f"  FAIL: {fn.__name__}: {exc}")
            failures.append(fn.__name__)
        except Exception as exc:
            print(f"  ERROR: {fn.__name__}: {exc}")
            failures.append(fn.__name__)

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print("All _probe_health tests passed.")
        sys.exit(0)
