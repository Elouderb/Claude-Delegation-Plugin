"""Startup-isolation regression tests (card 2f94ecee — the highest-value fix).

Background
---------
The task-cards MCP server registers all card tools at MODULE IMPORT and only
SERVES them once ``server.run()`` is reached. ``__main__`` runs
``ensure_agent_os()`` BEFORE ``run()``, and ``ensure_agent_os()`` ends with
``except Exception: raise`` — which ``__main__`` turns into ``sys.exit(1)``. So if
anything in ``ensure_agent_os()`` raised (or blocked long enough for the MCP client
to time out its initialize handshake), ``run()`` was never reached and EVERY tool —
card tools included — became inaccessible. Companion startup (the Flask graph
server + the outbox drain) runs in that pre-``run()`` path. On Windows a
``/reload-plugins`` leaves the old graph server holding :5000, so the new server's
companion-startup path hits a held/stale/foreign port. The fix isolates companion
startup so its outcome can NEVER take card tools down, and makes the graph-server
startup strictly time-bounded (stale-port recovery never hangs).

What these tests prove (both run cross-platform)
------------------------------------------------
* Test A: even when BOTH companion starts raise, ``ensure_agent_os()`` returns
  normally and the card tools are fully functional (create + list).
* Test B: with a REAL foreign process holding the graph port (pinned via
  AGENT_OS_GRAPH_PORT so the real ``start_graph_server`` runs its full conflict
  path), ``ensure_agent_os()`` still completes promptly (bounded — never hangs),
  spawns nothing on the pinned foreign port, and the card tools still respond.

Hermetic: AGENT_OS_HOME + cwd are redirected to temp dirs so no real ``~/.agent-os``
is touched and nothing binds :5000.
"""
from __future__ import annotations

import os
import socket
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

import graph_server  # noqa: E402
import server  # noqa: E402


class _StartupIsolationBase(unittest.TestCase):
    def setUp(self):
        # Redirect the machine-global home and the working dir to temp so neither
        # the real ~/.agent-os nor a real :5000 graph server is touched.
        self._tmp = TemporaryDirectory(prefix="agent_os_startup_iso_")
        self._base = Path(self._tmp.name)
        self._repo = self._base / "repo"
        self._home = self._base / "home"
        self._repo.mkdir()
        self._home.mkdir()

        self._prev_cwd = os.getcwd()
        self._prev_env = {
            k: os.environ.get(k)
            for k in ("AGENT_OS_HOME", "AGENT_OS_GRAPH_PORT", "AGENT_OS_SYNC")
        }
        os.environ["AGENT_OS_HOME"] = str(self._home)
        os.environ.pop("AGENT_OS_GRAPH_PORT", None)
        os.environ.pop("AGENT_OS_SYNC", None)  # keep the drain a no-op
        os.chdir(self._repo)

        # Snapshot graph-server module state so a test never leaks a spawned child
        # or a stale owned-port marker into a sibling test.
        self._prev_flask = graph_server.flask_process
        self._prev_owned = graph_server._owned_port
        graph_server.flask_process = None
        graph_server._owned_port = None
        self._prev_db_path = server.db_path

    def tearDown(self):
        # Never leak a real spawned graph server.
        proc = graph_server.flask_process
        if proc is not None and getattr(proc, "poll", lambda: 0)() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
        graph_server.flask_process = self._prev_flask
        graph_server._owned_port = self._prev_owned
        server.db_path = self._prev_db_path

        os.chdir(self._prev_cwd)
        for k, v in self._prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()


class TestCompanionFailureIsolation(_StartupIsolationBase):
    def test_card_tools_survive_both_companions_raising(self):
        """If BOTH companion starts raise, ensure_agent_os() must still succeed and
        the card tools must be fully usable."""
        with mock.patch.object(
            server, "start_graph_server", side_effect=RuntimeError("graph boom")
        ), mock.patch.object(
            server.sync_server, "start_sync_worker", side_effect=RuntimeError("sync boom")
        ):
            # Must NOT raise despite both companions blowing up.
            server.ensure_agent_os()

        card = server.create_card(
            title="isolation-card", description="proves tools work", priority="high"
        )
        self.assertIn("card_id", card)
        listed = server.list_cards()
        ids = [c["card_id"] for c in listed["cards"]]
        self.assertIn(
            card["card_id"], ids,
            "card tools are non-functional after companion startup failed — the "
            "startup-isolation guarantee is broken",
        )


class TestHeldPortDoesNotBlockCardTools(_StartupIsolationBase):
    def test_foreign_held_port_is_bounded_and_non_fatal(self):
        """A foreign process holding the (pinned) graph port must not hang startup,
        must not get a companion spawned onto it, and must not disable card tools."""
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        # Pin the port so the REAL start_graph_server runs its full conflict path and
        # (correctly) refuses to relocate off an operator-pinned port.
        os.environ["AGENT_OS_GRAPH_PORT"] = str(port)
        try:
            t0 = time.monotonic()
            server.ensure_agent_os()  # exercises the real graph-server startup
            elapsed = time.monotonic() - t0

            # Bounded: the health probe caps at a couple of seconds; assert we are
            # nowhere near a hang even on a loaded CI box.
            self.assertLess(
                elapsed, 15.0,
                f"ensure_agent_os() took {elapsed:.1f}s with a held port — startup "
                f"is not bounded",
            )
            # Nothing may be spawned onto a pinned, foreign-held port.
            self.assertIsNone(
                graph_server.flask_process,
                "a graph server was spawned onto a foreign-held pinned port",
            )
            # Card tools must work regardless.
            card = server.create_card(title="held-port-card")
            ids = [c["card_id"] for c in server.list_cards()["cards"]]
            self.assertIn(card["card_id"], ids)
        finally:
            holder.close()


if __name__ == "__main__":
    unittest.main()
