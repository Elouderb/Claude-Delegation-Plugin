"""CLI tests for mcp/task_inbox.py (card dadf614c): the down-sync entrypoint.

Hermetic: no live central DB, no real network. The HTTP transport is a
duck-typed ``_FakeClient`` swapped in for ``services.m3_client.http.Client``
(patched at its import site so ``task_inbox.main()``'s local
``from services.m3_client.http import Client`` picks it up) — never opens a
socket. ``python-dotenv``'s ``load_dotenv()`` is also patched to a no-op in
every test: the repo has a real root ``.env`` defining ``AGENT_OS_API_KEY``,
and letting it load would silently leak that value into env-resolution
assertions.

The local card DB IS real, but lives in a per-test ``TemporaryDirectory`` —
the same hermetic pattern ``test_task_projection.py`` uses for
``card_tools``/``graph_io`` global state.

Covers the card's required cases:
* ``--once`` invokes ``poll_once`` (via the fake client) and exits cleanly,
  applying tasks into the local card DB;
* ``--once`` on a poll failure logs and returns nonzero without raising;
* flag values override env values; env values are used when flags are omitted;
* a missing ``--source-repository-id`` (flag AND env) is a usage error;
* the loop mode is promptly interruptible by SIGINT and survives a transient
  per-poll error without exiting.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_MCP_DIR = _REPO_ROOT / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import card_tools  # noqa: E402
import graph_io  # noqa: E402
import task_inbox  # noqa: E402
from services.m3_client.http import ApiError  # noqa: E402


class _FakeClient:
    """Duck-typed stand-in for services.m3_client.http.Client. Records its
    constructor args and every get_tasks() call; serves scripted pages from
    ``script`` (a list of ``{"tasks": [...], "next_since": ...}`` bodies, or an
    Exception instance to raise) without ever touching the network."""

    # Class-level handoff read by __init__: set right before constructing an
    # instance through main() (which controls construction itself), then
    # copied into the instance's own _script so each instance is independent.
    _next_script: list = []

    def __init__(self, base_url, api_key=None, *, timeout=10.0):
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.get_tasks_calls = []
        self._script = list(_FakeClient._next_script)

    def get_tasks(self, *, repository_id=None, since=None, limit=200):
        self.get_tasks_calls.append(
            {"repository_id": repository_id, "since": since, "limit": limit}
        )
        if not self._script:
            return {"tasks": [], "next_since": None}
        page = self._script.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def _task_payload(global_id, *, title="From source", status="created", version=1):
    return {
        "global_id": global_id, "local_card_id": "origin01", "title": title,
        "description": None, "priority": "medium", "status": status, "version": version,
    }


class TaskInboxCliTestCase(unittest.TestCase):
    """Base: a temp repo root + isolated card_tools/graph_io state, dotenv
    disabled, and a fresh _FakeClient patched in for every test."""

    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="agent_os_inbox_cli_")
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()

        self._env_backup = {
            k: os.environ.get(k)
            for k in ("AGENT_OS_API_URL", "AGENT_OS_API_KEY", "AGENT_OS_SOURCE_REPOSITORY_ID")
        }
        for k in self._env_backup:
            os.environ.pop(k, None)

        _FakeClient._next_script = []
        self._client_patch = mock.patch("services.m3_client.http.Client", _FakeClient)
        self._client_patch.start()
        self._dotenv_patch = mock.patch("dotenv.load_dotenv", lambda *a, **k: None)
        self._dotenv_patch.start()

    def tearDown(self):
        self._dotenv_patch.stop()
        self._client_patch.stop()
        card_tools.set_db_path(None)
        graph_io.set_emission_repo_root(None)
        for k, v in self._env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _run(self, extra_args, script=None):
        _FakeClient._next_script = list(script or [])
        args = [
            "--once", "--repo-root", str(self.repo),
            "--api-url", "http://api.example.invalid",
            "--api-key", "flag-key",
            "--source-repository-id", "repo_src_0001",
        ] + extra_args
        return task_inbox.main(args)

class TestOnceMode(TaskInboxCliTestCase):
    def test_once_returns_zero_on_empty_page(self):
        rc = self._run([], script=[{"tasks": [], "next_since": None}])
        self.assertEqual(rc, 0)

    def test_once_applies_task_into_local_card_db(self):
        gid = "task_0001"
        rc = self._run(
            [],
            script=[{"tasks": [_task_payload(gid, title="Cross-machine")], "next_since": 5}],
        )
        self.assertEqual(rc, 0)
        cards = card_tools.list_cards()["cards"]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "Cross-machine")
        self.assertEqual(cards[0]["global_id"], gid)

    def test_once_poll_failure_logs_and_returns_nonzero_without_raising(self):
        rc = self._run([], script=[ApiError("boom", status=500)])
        self.assertEqual(rc, 1)


class TestEnvAndFlagResolution(TaskInboxCliTestCase):
    def _run_capturing_client(self, extra_args, env=None):
        env = env or {}
        for k, v in env.items():
            os.environ[k] = v
        captured = {}
        real_init = _FakeClient.__init__

        def spying_init(self, base_url, api_key=None, *, timeout=10.0):
            real_init(self, base_url, api_key, timeout=timeout)
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            captured["timeout"] = timeout
            captured["instance"] = self

        with mock.patch.object(_FakeClient, "__init__", spying_init):
            _FakeClient._next_script = [{"tasks": [], "next_since": None}]
            args = ["--once", "--repo-root", str(self.repo)] + extra_args
            rc = task_inbox.main(args)
        return rc, captured

    def test_flags_override_env(self):
        rc, captured = self._run_capturing_client(
            [
                "--api-url", "http://flag.example.invalid",
                "--api-key", "flag-key",
                "--source-repository-id", "flag-src",
            ],
            env={
                "AGENT_OS_API_URL": "http://env.example.invalid",
                "AGENT_OS_API_KEY": "env-key",
                "AGENT_OS_SOURCE_REPOSITORY_ID": "env-src",
            },
        )
        self.assertEqual(rc, 0)
        self.assertEqual(captured["base_url"], "http://flag.example.invalid")
        self.assertEqual(captured["api_key"], "flag-key")
        self.assertEqual(
            captured["instance"].get_tasks_calls[0]["repository_id"], "flag-src"
        )

    def test_env_used_when_flags_omitted(self):
        rc, captured = self._run_capturing_client(
            [],
            env={
                "AGENT_OS_API_URL": "http://env.example.invalid",
                "AGENT_OS_API_KEY": "env-key",
                "AGENT_OS_SOURCE_REPOSITORY_ID": "env-src",
            },
        )
        self.assertEqual(rc, 0)
        self.assertEqual(captured["base_url"], "http://env.example.invalid")
        self.assertEqual(captured["api_key"], "env-key")
        self.assertEqual(
            captured["instance"].get_tasks_calls[0]["repository_id"], "env-src"
        )

    def test_default_api_url_when_neither_flag_nor_env(self):
        rc, captured = self._run_capturing_client(
            ["--api-key", "k", "--source-repository-id", "src"]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(captured["base_url"], "http://127.0.0.1:8765")

    def test_missing_source_repository_id_is_a_usage_error(self):
        with self.assertRaises(SystemExit):
            task_inbox.main(
                ["--once", "--repo-root", str(self.repo),
                 "--api-url", "http://api.example.invalid", "--api-key", "k"]
            )


class TestLoopMode(TaskInboxCliTestCase):
    def setUp(self):
        super().setUp()
        self._orig_sigint = signal.getsignal(signal.SIGINT)
        self._orig_sigterm = signal.getsignal(signal.SIGTERM)

    def tearDown(self):
        signal.signal(signal.SIGINT, self._orig_sigint)
        signal.signal(signal.SIGTERM, self._orig_sigterm)
        super().tearDown()

    def test_loop_is_promptly_interruptible_by_sigint(self):
        client = _FakeClient("http://api.example.invalid", "k")
        client._script = []  # every poll returns an empty page

        def _send_sigint_soon():
            time.sleep(0.2)
            os.kill(os.getpid(), signal.SIGINT)

        sender = threading.Thread(target=_send_sigint_soon, daemon=True)
        sender.start()
        start = time.monotonic()
        task_inbox._run_loop(
            client, self.repo, "src", interval=10.0, apply_fn=lambda t: {"title": "x"}
        )
        elapsed = time.monotonic() - start
        sender.join(timeout=2)
        # Interrupted well before the 10s interval elapsed — proves the sleep
        # is a wait()-on-Event, not a blind time.sleep(interval).
        self.assertLess(elapsed, 5.0)

    def test_loop_survives_a_transient_poll_error(self):
        client = _FakeClient("http://api.example.invalid", "k")
        client._script = [ApiError("transient", status=503)]  # first poll fails

        def _send_sigint_soon():
            time.sleep(0.3)
            os.kill(os.getpid(), signal.SIGINT)

        sender = threading.Thread(target=_send_sigint_soon, daemon=True)
        sender.start()
        # Must return normally (no exception) despite the first poll raising.
        task_inbox._run_loop(
            client, self.repo, "src", interval=0.05, apply_fn=lambda t: {"title": "x"}
        )
        sender.join(timeout=2)
        # More than one poll happened: the transient error did not kill the loop.
        self.assertGreaterEqual(len(client.get_tasks_calls), 2)


if __name__ == "__main__":
    unittest.main()
