"""Drain-worker tests (card 8f84948b): stub HTTP server + real outbox files.

Exercises outbox.drain against a scriptable WSGI stub (via werkzeug's dev server
on an ephemeral loopback port — Windows-safe) and a real outbox seeded in a temp
repo. Proves the cursor-discipline invariants:

* a happy batch advances the cursor to EOF and leaves nothing pending;
* a malformed outbox line is dead-lettered locally and skipped (never sent);
* a server ``rejected`` verdict dead-letters the line but still resolves it;
* a 5xx / count-mismatch / connection-refused transport failure advances the
  cursor by NOTHING (no data loss) and reports failure;
* the ``AGENT_OS_SYNC_DISABLED`` kill-switch makes the worker a no-op;
* a backlog larger than the batch size is drained in multiple batches.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import sys
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from werkzeug.serving import make_server  # noqa: E402

from contracts import EVENT, new_id, utc_now  # noqa: E402
from contracts.events import EventEnvelope  # noqa: E402
from contracts.identity import (  # noqa: E402
    MachineIdentity,
    RepositoryIdentity,
    machine_identity_path,
    repository_identity_path,
)
from outbox import identity as ob_identity  # noqa: E402
from outbox import paths as ob_paths  # noqa: E402
from outbox import store as ob_store  # noqa: E402
from outbox import drain as ob_drain  # noqa: E402
from outbox import emit as ob_emit  # noqa: E402

logging.getLogger("werkzeug").setLevel(logging.ERROR)


# --------------------------------------------------------------------------- #
# Scriptable WSGI stub for the API
# --------------------------------------------------------------------------- #
class _Stub:
    """Records every request; registration succeeds; the ``/v1/events/batch``
    response is delegated to ``batch_handler(events)``.

    ``heartbeat_status`` overrides the heartbeat response code (to prove a failed
    heartbeat never blocks draining). ``repo_canonical_id`` makes the repository
    register reply with a DIFFERENT canonical id + ``remapped: true`` (to prove the
    drain rewrites outgoing repository_ids)."""

    def __init__(self, batch_handler, *, heartbeat_status=200, repo_canonical_id=None):
        self.batch_handler = batch_handler
        self.heartbeat_status = heartbeat_status
        self.repo_canonical_id = repo_canonical_id
        self.calls = []  # list of (path, body)

    def batch_calls(self):
        return [body for path, body in self.calls if path == "/v1/events/batch"]

    def __call__(self, environ, start_response):
        path = environ["PATH_INFO"]
        length = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {}
        self.calls.append((path, body))
        status, obj = self._route(path, body)
        data = json.dumps(obj).encode("utf-8")
        reason = http.client.responses.get(status, "OK")
        start_response(
            f"{status} {reason}",
            [("Content-Type", "application/json"), ("Content-Length", str(len(data)))],
        )
        return [data]

    def _route(self, path, body):
        if path == "/v1/machines/register":
            return 200, {"machine_id": body.get("machine_id"), "status": "created"}
        if path == "/v1/repositories/register":
            if self.repo_canonical_id is not None:
                return 200, {"repository_id": self.repo_canonical_id, "status": "updated",
                             "remapped": True}
            return 200, {"repository_id": body.get("repository_id"), "status": "created",
                         "remapped": False}
        if path == "/v1/sessions/register":
            return 200, {"session_id": body.get("session_id"), "status": "created"}
        if path.endswith("/heartbeat"):
            return self.heartbeat_status, {"result": "ok"}
        if path == "/v1/events/batch":
            return self.batch_handler(body.get("events", []))
        return 404, {"error": "not found"}


def accept_all(events):
    return 200, {"results": [
        {"index": i, "event_id": e.get("event_id"), "outcome": "accepted"}
        for i, e in enumerate(events)
    ]}


def reject_all(events):
    return 200, {"results": [
        {"index": i, "event_id": e.get("event_id"), "outcome": "rejected", "reason": "nope"}
        for i, e in enumerate(events)
    ]}


def fail_500(_events):
    return 500, {"error": "boom"}


def count_mismatch(_events):
    return 200, {"results": []}


@contextmanager
def serve(stub):
    server = make_server("127.0.0.1", 0, stub, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _free_port() -> int:
    """A port that is free right now (so a connection there is refused)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class DrainTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="agent_os_")
        tmp = Path(self._tmp.name)
        self.repo = tmp / "repo"
        self.repo.mkdir()
        self._home = tmp / "home"
        self._home.mkdir()
        self._prev_home = os.environ.get("AGENT_OS_HOME")
        os.environ["AGENT_OS_HOME"] = str(self._home)
        ob_emit._reset_ids_cache_for_tests()
        ob_identity.bootstrap_identity(self.repo)
        machine = MachineIdentity.model_validate_json(
            machine_identity_path().read_text(encoding="utf-8")
        )
        repo = RepositoryIdentity.model_validate_json(
            repository_identity_path(self.repo).read_text(encoding="utf-8")
        )
        self.machine_id = machine.machine_id
        self.repository_id = repo.repository_id

    def tearDown(self):
        if self._prev_home is None:
            os.environ.pop("AGENT_OS_HOME", None)
        else:
            os.environ["AGENT_OS_HOME"] = self._prev_home
        ob_emit._reset_ids_cache_for_tests()
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------- #
    def _seed_valid(self, count):
        for i in range(count):
            env = EventEnvelope(
                event_id=new_id(EVENT), event_type="task.created", occurred_at=utc_now(),
                machine_id=self.machine_id, repository_id=self.repository_id,
                task_id=f"card{i}", payload={"card_id": f"card{i}", "n": i},
            )
            ob_store.append_envelope(self.repo, env)

    def _seed_malformed(self, line):
        ob_store.append_line(self.repo, line)

    def _outbox_size(self):
        return os.path.getsize(ob_paths.outbox_path(self.repo))

    def _cursor(self):
        return ob_store.read_cursor(self.repo)

    def _dead_letter_lines(self):
        path = ob_paths.dead_letter_path(self.repo)
        if not path.exists():
            return []
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def _config(self, api_url, *, disabled=False, batch_size=500):
        return ob_drain.DrainConfig(
            api_url=api_url, api_key="k", repo_root=self.repo, timeout=3,
            backoff_initial=0.01, disabled=disabled, batch_size=batch_size,
        )

    # -- tests ------------------------------------------------------------- #
    def test_run_once_drains_and_advances_cursor(self):
        self._seed_valid(3)
        stub = _Stub(accept_all)
        with serve(stub) as url:
            rc = ob_drain.Drainer(self._config(url)).run_once()
        self.assertEqual(rc, 0)
        self.assertEqual(self._cursor(), self._outbox_size())
        # Registration happened, then exactly one batch of 3 events.
        paths_hit = [p for p, _ in stub.calls]
        self.assertIn("/v1/machines/register", paths_hit)
        self.assertIn("/v1/repositories/register", paths_hit)
        self.assertIn("/v1/sessions/register", paths_hit)
        self.assertEqual(len(stub.batch_calls()), 1)
        self.assertEqual(len(stub.batch_calls()[0]["events"]), 3)

    def test_malformed_line_is_dead_lettered_and_skipped(self):
        self._seed_valid(2)
        self._seed_malformed('{"not":"an envelope"}')
        stub = _Stub(accept_all)
        with serve(stub) as url:
            rc = ob_drain.Drainer(self._config(url)).run_once()
        self.assertEqual(rc, 0)
        self.assertEqual(self._cursor(), self._outbox_size())  # advanced past all 3 lines
        self.assertEqual(len(self._dead_letter_lines()), 1)
        # Only the 2 valid events were sent to the server.
        self.assertEqual(len(stub.batch_calls()[0]["events"]), 2)

    def test_rejected_events_are_dead_lettered_but_resolved(self):
        self._seed_valid(2)
        stub = _Stub(reject_all)
        with serve(stub) as url:
            rc = ob_drain.Drainer(self._config(url)).run_once()
        self.assertEqual(rc, 0)
        self.assertEqual(self._cursor(), self._outbox_size())  # rejected still resolves
        self.assertEqual(len(self._dead_letter_lines()), 2)

    def test_batch_5xx_holds_cursor(self):
        self._seed_valid(2)
        stub = _Stub(fail_500)
        with serve(stub) as url:
            rc = ob_drain.Drainer(self._config(url)).run_once()
        self.assertEqual(rc, 2)  # transport failure during drain
        self.assertEqual(self._cursor(), 0)  # cursor NOT advanced — no data loss

    def test_result_count_mismatch_holds_cursor(self):
        self._seed_valid(2)
        stub = _Stub(count_mismatch)
        with serve(stub) as url:
            rc = ob_drain.Drainer(self._config(url)).run_once()
        self.assertEqual(rc, 2)
        self.assertEqual(self._cursor(), 0)

    def test_server_down_holds_cursor(self):
        self._seed_valid(2)
        url = f"http://127.0.0.1:{_free_port()}"  # nothing listening -> refused
        rc = ob_drain.Drainer(self._config(url)).run_once()
        self.assertEqual(rc, 1)  # could not even register
        self.assertEqual(self._cursor(), 0)

    def test_kill_switch_is_noop(self):
        self._seed_valid(2)
        stub = _Stub(accept_all)
        with serve(stub) as url:
            rc = ob_drain.Drainer(self._config(url, disabled=True)).run_once()
        self.assertEqual(rc, 0)
        self.assertEqual(stub.calls, [])  # no network at all
        self.assertEqual(self._cursor(), 0)  # nothing drained

    def test_env_kill_switch_via_config_from_env(self):
        prev = os.environ.get("AGENT_OS_SYNC_DISABLED")
        os.environ["AGENT_OS_SYNC_DISABLED"] = "1"
        try:
            cfg = ob_drain.config_from_env(repo_root=self.repo)
            self.assertTrue(cfg.disabled)
        finally:
            if prev is None:
                os.environ.pop("AGENT_OS_SYNC_DISABLED", None)
            else:
                os.environ["AGENT_OS_SYNC_DISABLED"] = prev

    def test_backlog_drains_in_multiple_batches(self):
        self._seed_valid(5)
        stub = _Stub(accept_all)
        with serve(stub) as url:
            rc = ob_drain.Drainer(self._config(url, batch_size=2)).run_once()
        self.assertEqual(rc, 0)
        self.assertEqual(self._cursor(), self._outbox_size())
        # 5 events, batch size 2 -> batches of 2, 2, 1.
        sizes = [len(b["events"]) for b in stub.batch_calls()]
        self.assertEqual(sizes, [2, 2, 1])

    def test_outage_retry_dead_letters_each_bad_line_exactly_once(self):
        # A malformed line + a transient outage: the batch fails repeatedly, then
        # succeeds. The bad line must be dead-lettered exactly ONCE (dead-lettering
        # is deferred until the batch resolves), never once per retry.
        self._seed_valid(1)
        self._seed_malformed('{"not":"an envelope"}')
        attempts = {"n": 0}

        def flaky(events):
            attempts["n"] += 1
            if attempts["n"] < 3:  # first two attempts fail (transport outage)
                return 500, {"error": {"code": "boom", "message": "down"}}
            return 200, {"results": [{"index": i, "event_id": e.get("event_id"),
                                      "outcome": "accepted"} for i, e in enumerate(events)]}

        stub = _Stub(flaky)
        with serve(stub) as url:
            drainer = ob_drain.Drainer(self._config(url))
            self.assertTrue(drainer.register(attempts=1))
            outcomes = []
            for _ in range(5):
                try:
                    outcomes.append(drainer.drain_batch())
                except ob_drain.TransportError:
                    outcomes.append("transport-error")
            # Two failures, then progress, then empty.
            self.assertEqual(outcomes[:4], ["transport-error", "transport-error",
                                            "progress", "empty"])
        self.assertEqual(len(self._dead_letter_lines()), 1)  # exactly one, not one/retry
        self.assertEqual(self._cursor(), self._outbox_size())

    def test_remapped_repository_id_is_rewritten_in_outgoing_events(self):
        self._seed_valid(2)
        canonical = "repo_canonical_0001"
        self.assertNotEqual(canonical, self.repository_id)
        stub = _Stub(accept_all, repo_canonical_id=canonical)
        with serve(stub) as url:
            rc = ob_drain.Drainer(self._config(url)).run_once()
        self.assertEqual(rc, 0)
        # Events were seeded with the LOCAL repository_id; the drain learned the
        # canonical id from registration (remapped=true) and rewrote every event.
        sent = stub.batch_calls()[0]["events"]
        self.assertTrue(all(e["repository_id"] == canonical for e in sent))
        self.assertTrue(all(e["repository_id"] != self.repository_id for e in sent))

    def test_heartbeat_failure_does_not_block_draining(self):
        # Heartbeat 500s but the batch accepts: draining must still complete and
        # advance the cursor (heartbeat is best-effort, isolated from the drain).
        self._seed_valid(2)
        stub = _Stub(accept_all, heartbeat_status=500)
        with serve(stub) as url:
            rc = ob_drain.Drainer(self._config(url)).run_once()
        self.assertEqual(rc, 0)
        self.assertEqual(self._cursor(), self._outbox_size())

    def test_bounded_read_passes_max_bytes(self):
        # drain_batch must bound the disk read (max_bytes), not slurp the whole tail.
        self._seed_valid(3)
        captured = {}
        real_read_pending = ob_store.read_pending

        def spy(repo_root, *args, **kwargs):
            captured.update(kwargs)
            return real_read_pending(repo_root, *args, **kwargs)

        stub = _Stub(accept_all)
        with serve(stub) as url:
            drainer = ob_drain.Drainer(self._config(url, batch_size=7))
            self.assertTrue(drainer.register(attempts=1))
            ob_drain.store.read_pending = spy
            try:
                drainer.drain_batch()
            finally:
                ob_drain.store.read_pending = real_read_pending
        self.assertEqual(captured.get("max_bytes"), 7 * ob_store.MAX_LINE_BYTES)
        self.assertEqual(captured.get("max_events"), 7)

    def test_remint_fresh_home_registers_without_looping(self):
        # Simulate a re-minted machine identity: wipe AGENT_OS_HOME (so a new
        # machine id is minted) while the repo identity file persists with the OLD
        # machine_id. The drain must send the CURRENT machine id and register
        # cleanly (no permanent register-fail loop), then drain.
        self._seed_valid(1)
        import shutil
        shutil.rmtree(self._home)  # drop the machine identity -> re-mint on next load
        self._home.mkdir()
        stub = _Stub(accept_all)
        with serve(stub) as url:
            rc = ob_drain.Drainer(self._config(url)).run_once()
        self.assertEqual(rc, 0)
        # The repo was registered under the freshly-minted machine id, not the
        # stale one embedded in the repo identity file.
        repo_reg = [b for p, b in stub.calls if p == "/v1/repositories/register"][0]
        machine_reg = [b for p, b in stub.calls if p == "/v1/machines/register"][0]
        self.assertEqual(repo_reg["machine_id"], machine_reg["machine_id"])


class TestPostJson(unittest.TestCase):
    def test_transport_error_on_closed_port(self):
        url = f"http://127.0.0.1:{_free_port()}/v1/events/batch"
        with self.assertRaises(ob_drain.TransportError):
            ob_drain.post_json(url, "k", {"events": []}, timeout=1)

    def test_http_error_status_is_returned_not_raised(self):
        stub = _Stub(fail_500)
        with serve(stub) as base:
            status, body = ob_drain.post_json(f"{base}/v1/events/batch", "k",
                                              {"events": []}, timeout=3)
        self.assertEqual(status, 500)
        self.assertEqual(body.get("error"), "boom")


if __name__ == "__main__":
    unittest.main()
