"""Tests for scripts/sync_harness.py (card f20398be): the cross-machine sync
test harness CLI (``new-card`` / ``up`` / ``down`` / ``status`` / ``roundtrip``).

Hermetic: no live database, no real network. HTTP is served by the REAL Flask
app (``services.api.app.create_app``) over an in-memory ``FakeStore``, bound to
a loopback socket via werkzeug -- the same pattern
``mcp/tests/test_m3_client.py`` and ``mcp/tests/test_drain.py`` already use for
HTTP-touching tests in this plugin, so ``up``/``down``/``status`` are exercised
against the real ``/v1`` contract rather than a hand-rolled mirror of it (the
card's own "mock the HTTP Client / FakeStore" requirement). Machine identity is
isolated per test via ``AGENT_OS_HOME`` (mirrors
``mcp/tests/test_drain.py``'s ``DrainTestCase``) so these tests never touch the
real developer machine's ``~/.agent-os`` identity, and ``python-dotenv`` is
patched to a no-op (mirrors ``mcp/tests/test_task_inbox_cli.py``) so the repo's
real root ``.env`` (which defines a real ``AGENT_OS_API_KEY``) never leaks into
an env-resolution assertion.

Covers the card's required cases:
* ``new-card`` creates a local card AND actually queues its ``task.created``
  outbox event (the identity-bootstrap fix this card needed -- without it,
  ``emit_event`` silently no-ops on a fresh repo and nothing would ever drain);
* ``status`` rendering, as a pure function over a hand-built snapshot dict,
  and end-to-end against the real (fake-backed) API;
* flag/env precedence for ``--api-url``/``--api-key`` (flags override env,
  env used when flags omitted, default when neither given);
* ``down``'s ``--source-repository-id`` requirement is a reported failure
  (exit 1), not a raised exception;
* ``roundtrip`` composes ``new-card`` -> ``up`` -> ``status`` and stops at the
  first failure;
* a full two-repo up/down/status loop: a card created and drained on one repo
  is pulled and applied on a SEPARATE repo, and that repo's ``status`` shows
  the local card matching the central task.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_MCP_DIR = _REPO_ROOT / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from werkzeug.serving import make_server  # noqa: E402

from services.api.app import create_app  # noqa: E402
from services.api.store import FakeStore  # noqa: E402
from outbox import emit as ob_emit  # noqa: E402
from outbox import store as ob_store  # noqa: E402

import card_tools  # noqa: E402
import graph_io  # noqa: E402
import sync_harness  # noqa: E402  # pyright: ignore[reportMissingImports] -- see scripts/sync_harness.py

_KEY = "harness-test-key"
_ENV_KEYS = (
    "AGENT_OS_API_URL", "AGENT_OS_API_KEY", "AGENT_OS_SOURCE_REPOSITORY_ID",
    "AGENT_OS_EVENTS_DISABLED", "AGENT_OS_SYNC_DISABLED",
)


@contextlib.contextmanager
def serve_api(*, store=None, api_key=_KEY):
    """A real Agent OS API app (FakeStore-backed) on a real loopback port."""
    app = create_app(api_key=api_key, store=store if store is not None else FakeStore())
    app.testing = True
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _HarnessTestCase(unittest.TestCase):
    """Shared isolation for every test below: a temp ``AGENT_OS_HOME``,
    cleared sync env vars, disabled dotenv, and card_tools/graph_io/identity
    global-state reset -- this harness wraps both outbox.drain and
    mcp/task_inbox.py, so it needs both modules' test isolation combined."""

    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="agent_os_sync_harness_")
        self.tmp_path = Path(self._tmp.name)
        self.repo = self._new_repo("repo")
        self._home = self.tmp_path / "home"
        self._home.mkdir()

        self._prev_home = os.environ.get("AGENT_OS_HOME")
        os.environ["AGENT_OS_HOME"] = str(self._home)

        self._env_backup = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in self._env_backup:
            os.environ.pop(k, None)

        ob_emit._reset_ids_cache_for_tests()
        self._dotenv_patch = mock.patch("dotenv.load_dotenv", lambda *a, **k: None)
        self._dotenv_patch.start()

    def tearDown(self):
        self._dotenv_patch.stop()
        card_tools.set_db_path(None)
        graph_io.set_emission_repo_root(None)
        ob_emit._reset_ids_cache_for_tests()
        if self._prev_home is None:
            os.environ.pop("AGENT_OS_HOME", None)
        else:
            os.environ["AGENT_OS_HOME"] = self._prev_home
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _new_repo(self, name: str) -> Path:
        path = self.tmp_path / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = sync_harness.main(argv)
        return rc, out.getvalue(), err.getvalue()


# --------------------------------------------------------------------------- #
# new-card
# --------------------------------------------------------------------------- #
class TestNewCard(_HarnessTestCase):
    def test_creates_card_and_queues_sync_event(self):
        rc, out, _err = self._run(
            ["new-card", "--repo-root", str(self.repo), "--title", "Harness card"]
        )
        self.assertEqual(rc, 0)
        self.assertIn("new-card: card_id=", out)
        self.assertIn("Harness card", out)

        cards = card_tools.list_cards()["cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "Harness card")
        self.assertIsNotNone(cards[0]["global_id"])

        # The identity-bootstrap fix (card f20398be): without it this queue
        # would be empty because emit_event silently no-ops on a fresh repo.
        pending = ob_store.read_pending(self.repo, max_events=10, max_bytes=1_000_000)
        self.assertEqual(len(pending), 1)
        envelope = pending[0].envelope
        self.assertIsNotNone(envelope)
        assert envelope is not None  # narrows for pyright; asserted non-None above
        self.assertEqual(envelope.event_type, "task.created")

    def test_default_title_used_when_omitted(self):
        rc, _out, _err = self._run(["new-card", "--repo-root", str(self.repo)])
        self.assertEqual(rc, 0)
        cards = card_tools.list_cards()["cards"]
        self.assertEqual(cards[0]["title"], sync_harness._DEFAULT_TITLE)


# --------------------------------------------------------------------------- #
# render_status: pure function
# --------------------------------------------------------------------------- #
class TestRenderStatus(unittest.TestCase):
    def test_normal_snapshot(self):
        data = {
            "repo_root": "/x", "api_url": "http://api",
            "local": {
                "total": 1,
                "recent": [{"card_id": "c1", "global_id": "task_1", "status": "Created", "title": "T"}],
            },
            "central": {
                "total": 1, "error": None,
                "recent": [{"global_id": "task_1", "status": "created", "title": "T"}],
            },
            "machines": {
                "total": 1, "error": None,
                "items": [{"machine_id": "m1", "name": "n", "os": "Linux"}],
            },
        }
        text = sync_harness.render_status(data)
        self.assertIn("LOCAL CARDS (1)", text)
        self.assertIn("CENTRAL TASKS (1)", text)
        self.assertIn("MACHINES (1)", text)
        self.assertIn("glance: 1/1", text)

    def test_empty_sections_say_none(self):
        data = {
            "repo_root": "/x", "api_url": "http://api",
            "local": {"total": 0, "recent": []},
            "central": {"total": 0, "recent": [], "error": None},
            "machines": {"total": 0, "items": [], "error": None},
        }
        text = sync_harness.render_status(data)
        self.assertIn("(none)", text)
        self.assertIn("glance: 0/0", text)

    def test_reports_central_and_machine_errors_without_a_glance_line(self):
        data = {
            "repo_root": "/x", "api_url": "http://api",
            "local": {"total": 0, "recent": []},
            "central": {"total": None, "recent": [], "error": "boom"},
            "machines": {"total": None, "items": [], "error": "boom2"},
        }
        text = sync_harness.render_status(data)
        self.assertIn("CENTRAL TASKS: unavailable (boom)", text)
        self.assertIn("MACHINES: unavailable (boom2)", text)
        self.assertNotIn("glance:", text)


# --------------------------------------------------------------------------- #
# _env_overrides
# --------------------------------------------------------------------------- #
class TestEnvOverrides(unittest.TestCase):
    def test_sets_and_restores(self):
        os.environ["SYNC_HARNESS_TEST_FOO"] = "orig"
        try:
            with sync_harness._env_overrides(
                {"SYNC_HARNESS_TEST_FOO": "new", "SYNC_HARNESS_TEST_BAR": "created"}
            ):
                self.assertEqual(os.environ["SYNC_HARNESS_TEST_FOO"], "new")
                self.assertEqual(os.environ["SYNC_HARNESS_TEST_BAR"], "created")
            self.assertEqual(os.environ["SYNC_HARNESS_TEST_FOO"], "orig")
            self.assertNotIn("SYNC_HARNESS_TEST_BAR", os.environ)
        finally:
            os.environ.pop("SYNC_HARNESS_TEST_FOO", None)
            os.environ.pop("SYNC_HARNESS_TEST_BAR", None)


# --------------------------------------------------------------------------- #
# arg / env resolution (status, via a duck-typed fake Client)
# --------------------------------------------------------------------------- #
class _FakeClient:
    def __init__(self, base_url, api_key=None, *, timeout=10.0):
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout

    def get_tasks(self, *, repository_id=None, since=None, limit=200):
        return {"tasks": [], "next_since": None}

    def get_machines(self):
        return []


class TestStatusEnvAndFlagResolution(_HarnessTestCase):
    def _run_capturing_client(self, extra_args, env=None):
        for k, v in (env or {}).items():
            os.environ[k] = v
        captured = {}
        real_init = _FakeClient.__init__

        def spying_init(self, base_url, api_key=None, *, timeout=10.0):
            real_init(self, base_url, api_key, timeout=timeout)
            captured["base_url"] = base_url
            captured["api_key"] = api_key

        with mock.patch.object(_FakeClient, "__init__", spying_init):
            with mock.patch("services.m3_client.http.Client", _FakeClient):
                rc, out, _err = self._run(
                    ["status", "--repo-root", str(self.repo)] + extra_args
                )
        return rc, out, captured

    def test_flags_override_env(self):
        rc, _out, captured = self._run_capturing_client(
            ["--api-url", "http://flag.example.invalid", "--api-key", "flag-key"],
            env={
                "AGENT_OS_API_URL": "http://env.example.invalid",
                "AGENT_OS_API_KEY": "env-key",
            },
        )
        self.assertEqual(rc, 0)
        self.assertEqual(captured["base_url"], "http://flag.example.invalid")
        self.assertEqual(captured["api_key"], "flag-key")

    def test_env_used_when_flags_omitted(self):
        rc, _out, captured = self._run_capturing_client(
            [],
            env={
                "AGENT_OS_API_URL": "http://env.example.invalid",
                "AGENT_OS_API_KEY": "env-key",
            },
        )
        self.assertEqual(rc, 0)
        self.assertEqual(captured["base_url"], "http://env.example.invalid")
        self.assertEqual(captured["api_key"], "env-key")

    def test_default_api_url_when_neither_flag_nor_env(self):
        rc, _out, captured = self._run_capturing_client([])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["base_url"], sync_harness._DEFAULT_URL)


# --------------------------------------------------------------------------- #
# down: --source-repository-id is a reported failure, not a raised one
# --------------------------------------------------------------------------- #
class TestDownRequiresSourceRepositoryId(_HarnessTestCase):
    def test_missing_flag_and_env_returns_one_with_a_clear_message(self):
        rc, _out, err = self._run(
            ["down", "--repo-root", str(self.repo), "--api-url", "http://x.invalid"]
        )
        self.assertEqual(rc, 1)
        self.assertIn("source-repository-id", err)


# --------------------------------------------------------------------------- #
# End-to-end: real (FakeStore-backed) API server
# --------------------------------------------------------------------------- #
class TestEndToEnd(_HarnessTestCase):
    def test_up_then_down_on_a_separate_repo_then_status_shows_the_match(self):
        with serve_api() as base_url:
            source_repo = self.repo
            dest_repo = self._new_repo("repo_dest")

            rc, _out, _err = self._run(
                ["new-card", "--repo-root", str(source_repo), "--title", "cross-repo card"]
            )
            self.assertEqual(rc, 0)

            rc, out, _err = self._run(
                ["up", "--repo-root", str(source_repo),
                 "--api-url", base_url, "--api-key", _KEY]
            )
            self.assertEqual(rc, 0)
            self.assertIn("up: drain succeeded", out)

            from services.m3_client.http import Client

            client = Client(base_url, _KEY)
            matches = [
                r["repository_id"] for r in client.get_repositories()
                if r["repo_root"] == str(source_repo)
            ]
            self.assertEqual(len(matches), 1)
            source_repository_id = matches[0]

            rc, out, _err = self._run(
                ["down", "--repo-root", str(dest_repo),
                 "--api-url", base_url, "--api-key", _KEY,
                 "--source-repository-id", source_repository_id]
            )
            self.assertEqual(rc, 0)
            self.assertIn("down: pull+apply succeeded", out)

            rc, out, _err = self._run(
                ["status", "--repo-root", str(dest_repo),
                 "--api-url", base_url, "--api-key", _KEY]
            )
            self.assertEqual(rc, 0)
            self.assertIn("LOCAL CARDS (1)", out)
            self.assertIn("cross-repo card", out)
            self.assertIn("glance: 1/1", out)

    def test_roundtrip_via_env_vars_only(self):
        with serve_api() as base_url:
            os.environ["AGENT_OS_API_URL"] = base_url
            os.environ["AGENT_OS_API_KEY"] = _KEY
            rc, out, _err = self._run(
                ["roundtrip", "--repo-root", str(self.repo), "--title", "env roundtrip"]
            )
            self.assertEqual(rc, 0)
            self.assertIn("== new-card ==", out)
            self.assertIn("== up ==", out)
            self.assertIn("== status ==", out)
            self.assertIn("glance: 1/1", out)

    def test_roundtrip_stops_before_status_when_up_fails(self):
        # An address nothing listens on: register() retries a few times with
        # backoff before drain.main() gives up, so this test costs a few
        # seconds -- accepted for exercising the real failure path end-to-end.
        unreachable = f"http://127.0.0.1:{_free_port()}"
        rc, out, _err = self._run(
            ["roundtrip", "--repo-root", str(self.repo),
             "--api-url", unreachable, "--api-key", _KEY]
        )
        self.assertNotEqual(rc, 0)
        self.assertIn("== new-card ==", out)
        self.assertIn("== up ==", out)
        self.assertNotIn("== status ==", out)


# --------------------------------------------------------------------------- #
# CLI parser
# --------------------------------------------------------------------------- #
class TestParser(unittest.TestCase):
    def test_requires_a_subcommand(self):
        with self.assertRaises(SystemExit):
            sync_harness.build_parser().parse_args([])


if __name__ == "__main__":
    unittest.main()
