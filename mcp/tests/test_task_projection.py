"""Task down-projection tests (card ffcaf548, Phase 2a).

Proves the DOWN half of task sync end-to-end, hermetically (no real DB, no real
network beyond an ephemeral loopback port):

* central materializer: a task.* event with a global_id upserts one registry.tasks
  row; idempotent + last-write-wins on version; carries version / originating_client
  / repository_id; inert without a global_id;
* GET /v1/tasks: repository filter, keyset (since/next_since) paging, auth required,
  stable order;
* card_tools.apply_task_projection: creates + updates a local card from a
  TaskRecord; idempotent (keyed by global_id, keeps the local card_id); sets
  global_id; NEVER re-emits a task event (no sync loop);
* the full local demo: create_card in DB-A -> outbox -> the REAL create_app over a
  loopback port (FakeStore) -> central materialization -> GET /v1/tasks -> apply
  into a SECOND local card DB-B -> the card is visible (title/status) in DB-B.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# The MCP sub-modules (card_tools, task_inbox, graph_io) import each other by bare
# name, mirroring the running server's sys.path (server.py adds the repo root; the
# tool modules live in mcp/). Add mcp/ so they resolve here too.
_MCP_DIR = _REPO_ROOT / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

from werkzeug.serving import make_server  # noqa: E402

from contracts import EVENT, MACHINE, REPOSITORY, new_id, utc_now  # noqa: E402
from contracts.identity import (  # noqa: E402
    RepositoryIdentity,
    repository_identity_path,
)
from services.api.app import create_app  # noqa: E402
from services.api.store import FakeStore  # noqa: E402
from services.m3_client.http import Client  # noqa: E402

import card_tools  # noqa: E402
import graph_io  # noqa: E402
import task_inbox  # noqa: E402
from outbox import drain as ob_drain  # noqa: E402
from outbox import emit as ob_emit  # noqa: E402
from outbox import identity as ob_identity  # noqa: E402

logging.getLogger("werkzeug").setLevel(logging.ERROR)

_KEY = "test-api-key"
_AUTH = {"Authorization": f"Bearer {_KEY}"}


def _client(store=None):
    store = store if store is not None else FakeStore()
    app = create_app(api_key=_KEY, store=store)
    app.testing = True
    return app.test_client(), store


def _task_event(machine_id, repository_id, *, global_id, version=1, status="Created",
                title="Impl auth", priority="high", event_type="task.created",
                card_id="ab12cd34", description=None):
    payload = {
        "card_id": card_id, "title": title, "status": status, "priority": priority,
        "global_id": global_id, "version": version,
    }
    if description is not None:
        payload["description"] = description
    return {
        "event_id": new_id(EVENT), "event_type": event_type,
        "occurred_at": utc_now().isoformat(), "machine_id": machine_id,
        "repository_id": repository_id, "task_id": card_id, "payload": payload,
    }


# --------------------------------------------------------------------------- #
# 1. Central materializer (through the real /v1/events/batch + FakeStore)
# --------------------------------------------------------------------------- #
class TestCentralTaskMaterialization(unittest.TestCase):
    def setUp(self):
        self.client, self.store = _client()
        self.machine_id, self.repo_id = new_id(MACHINE), new_id(REPOSITORY)

    def _post(self, *events):
        r = self.client.post("/v1/events/batch", headers=_AUTH, json={"events": list(events)})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r

    def _tasks(self, **params):
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        url = "/v1/tasks" + (f"?{qs}" if qs else "")
        return self.client.get(url, headers=_AUTH).get_json()["tasks"]

    def test_task_event_materializes_one_row_with_metadata(self):
        gid = new_id_task()
        self._post(_task_event(self.machine_id, self.repo_id, global_id=gid))
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        row = tasks[0]
        self.assertEqual(row["global_id"], gid)
        self.assertEqual(row["status"], "created")            # normalized TaskSyncStatus
        self.assertEqual(row["title"], "Impl auth")
        self.assertEqual(row["version"], 1)
        self.assertEqual(row["originating_client"], self.machine_id)
        self.assertEqual(row["repository_id"], self.repo_id)
        self.assertEqual(row["local_card_id"], "ab12cd34")
        self.assertIn("change_seq", row)

    def test_inert_without_global_id(self):
        # A pre-Phase-2a task event (no global_id) is accepted into the event log
        # but materializes NO task row.
        evt = _task_event(self.machine_id, self.repo_id, global_id="x")
        evt["payload"].pop("global_id")
        self._post(evt)
        self.assertEqual(self._tasks(), [])

    def test_idempotent_and_last_write_wins(self):
        gid = new_id_task()
        # created (v1) then updated (v2, In Progress): still ONE row, latest state.
        self._post(_task_event(self.machine_id, self.repo_id, global_id=gid, version=1))
        self._post(_task_event(self.machine_id, self.repo_id, global_id=gid, version=2,
                               status="In Progress", event_type="task.updated"))
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "in_progress")
        self.assertEqual(tasks[0]["version"], 2)
        seq_after_v2 = tasks[0]["change_seq"]

        # An out-of-order OLDER event (v1) must NOT clobber the newer state.
        self._post(_task_event(self.machine_id, self.repo_id, global_id=gid, version=1,
                               status="Created", event_type="task.updated"))
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["status"], "in_progress")  # unchanged
        self.assertEqual(tasks[0]["version"], 2)
        self.assertEqual(tasks[0]["change_seq"], seq_after_v2)  # no spurious re-stamp

    def test_repository_filter_and_keyset_paging(self):
        other_repo = new_id(REPOSITORY)
        g1, g2, g3 = new_id_task(), new_id_task(), new_id_task()
        self._post(_task_event(self.machine_id, self.repo_id, global_id=g1))
        self._post(_task_event(self.machine_id, self.repo_id, global_id=g2))
        self._post(_task_event(self.machine_id, other_repo, global_id=g3))

        # repository filter: only this repo's two tasks.
        mine = self._tasks(repository_id=self.repo_id)
        self.assertEqual({t["global_id"] for t in mine}, {g1, g2})

        # keyset: page size 1, walk forward with next_since, no overlap / no drops.
        body1 = self.client.get(
            f"/v1/tasks?repository_id={self.repo_id}&limit=1", headers=_AUTH
        ).get_json()
        self.assertEqual(len(body1["tasks"]), 1)
        first = body1["tasks"][0]["global_id"]
        nxt = body1["next_since"]
        self.assertIsNotNone(nxt)
        body2 = self.client.get(
            f"/v1/tasks?repository_id={self.repo_id}&limit=1&since={nxt}", headers=_AUTH
        ).get_json()
        self.assertEqual(len(body2["tasks"]), 1)
        second = body2["tasks"][0]["global_id"]
        self.assertNotEqual(first, second)
        self.assertTrue(all(t["change_seq"] > nxt for t in body2["tasks"]))
        # caught up: nothing strictly newer than the last page.
        body3 = self.client.get(
            f"/v1/tasks?repository_id={self.repo_id}&since={body2['next_since']}", headers=_AUTH
        ).get_json()
        self.assertEqual(body3["tasks"], [])
        self.assertIsNone(body3["next_since"])

    def test_tasks_requires_auth(self):
        self.assertEqual(self.client.get("/v1/tasks").status_code, 401)


def new_id_task():
    from contracts import TASK

    return new_id(TASK)


# --------------------------------------------------------------------------- #
# 2. Local apply (card_tools.apply_task_projection) + no-emit invariant
# --------------------------------------------------------------------------- #
class _LocalCardBase(unittest.TestCase):
    """A temp card DB + temp AGENT_OS_HOME with identity bootstrapped, so that a
    (forbidden) emission from apply WOULD land in the outbox and be detectable."""

    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="agent_os_apply_")
        tmp = Path(self._tmp.name)
        self.repo = tmp / "repo"
        self.repo.mkdir()
        self._home = tmp / "home"
        self._home.mkdir()
        self._prev_home = os.environ.get("AGENT_OS_HOME")
        os.environ["AGENT_OS_HOME"] = str(self._home)
        ob_emit._reset_ids_cache_for_tests()
        ob_identity.bootstrap_identity(self.repo)

        agent_os = self.repo / ".agent-os"
        agent_os.mkdir(exist_ok=True)
        card_tools.set_db_path(agent_os / "cards.sqlite")
        card_tools.init_db()
        graph_io.set_emission_repo_root(self.repo)

    def tearDown(self):
        card_tools.set_db_path(None)
        graph_io.set_emission_repo_root(None)
        ob_emit._reset_ids_cache_for_tests()
        if self._prev_home is None:
            os.environ.pop("AGENT_OS_HOME", None)
        else:
            os.environ["AGENT_OS_HOME"] = self._prev_home
        self._tmp.cleanup()

    def _outbox_lines(self):
        path = self.repo / ".agent-os" / "sync" / "outbox.jsonl"
        if not path.exists():
            return []
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


class TestApplyTaskProjection(_LocalCardBase):
    def _projection(self, gid, *, title, status, version, priority="high", description=None):
        return {
            "global_id": gid, "local_card_id": "origin01", "title": title,
            "description": description, "priority": priority, "status": status,
            "version": version,
        }

    def test_create_then_idempotent_update_keeps_one_card(self):
        gid = new_id_task()
        created = card_tools.apply_task_projection(
            self._projection(gid, title="Sync me", status="created", version=1)
        )
        self.assertNotIn("error", created)
        self.assertEqual(created["title"], "Sync me")
        self.assertEqual(created["status"], "Created")          # sync->local mapping
        self.assertEqual(created["global_id"], gid)
        self.assertEqual(created["priority"], "high")
        card_id = created["card_id"]

        # A second apply (status advanced) updates the SAME card in place.
        updated = card_tools.apply_task_projection(
            self._projection(gid, title="Sync me", status="in_progress", version=2)
        )
        self.assertEqual(updated["card_id"], card_id)           # no duplicate
        self.assertEqual(updated["status"], "In Progress")
        self.assertEqual(card_tools.list_cards()["total"], 1)

    def test_blocked_maps_to_in_progress(self):
        gid = new_id_task()
        res = card_tools.apply_task_projection(
            self._projection(gid, title="B", status="blocked", version=1)
        )
        self.assertEqual(res["status"], "In Progress")

    def test_missing_global_id_is_error_not_raise(self):
        res = card_tools.apply_task_projection({"title": "no id", "status": "created"})
        self.assertIn("error", res)

    def test_apply_never_emits_a_task_event(self):
        # Baseline: creating a card locally DOES emit (proves the outbox is wired).
        card_tools.create_card(title="local", priority="low")
        self.assertGreaterEqual(len(self._outbox_lines()), 1)
        before = len(self._outbox_lines())

        # Applying a down-projection must add NOTHING to the outbox (no sync loop).
        gid = new_id_task()
        card_tools.apply_task_projection(
            self._projection(gid, title="down", status="created", version=1)
        )
        self.assertEqual(len(self._outbox_lines()), before)


# --------------------------------------------------------------------------- #
# 3. End-to-end local demo: DB-A -> drain -> central -> pull-apply -> DB-B
# --------------------------------------------------------------------------- #
@contextmanager
def _serve(app):
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


class TestEndToEndLocalProjection(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="agent_os_e2e_")
        tmp = Path(self._tmp.name)
        self.repo_a = tmp / "repoA"
        self.repo_b = tmp / "repoB"
        self.repo_a.mkdir()
        self.repo_b.mkdir()
        self._home = tmp / "home"
        self._home.mkdir()
        self._prev_home = os.environ.get("AGENT_OS_HOME")
        os.environ["AGENT_OS_HOME"] = str(self._home)
        ob_emit._reset_ids_cache_for_tests()
        # Distinct repo identities (same machine) for A and B.
        ob_identity.bootstrap_identity(self.repo_a)
        ob_identity.bootstrap_identity(self.repo_b)
        self.repo_a_id = RepositoryIdentity.model_validate_json(
            repository_identity_path(self.repo_a).read_text(encoding="utf-8")
        ).repository_id
        for repo in (self.repo_a, self.repo_b):
            (repo / ".agent-os").mkdir(exist_ok=True)
        self.db_a = self.repo_a / ".agent-os" / "cards.sqlite"
        self.db_b = self.repo_b / ".agent-os" / "cards.sqlite"

    def tearDown(self):
        card_tools.set_db_path(None)
        graph_io.set_emission_repo_root(None)
        ob_emit._reset_ids_cache_for_tests()
        if self._prev_home is None:
            os.environ.pop("AGENT_OS_HOME", None)
        else:
            os.environ["AGENT_OS_HOME"] = self._prev_home
        self._tmp.cleanup()

    def _use_repo_a(self):
        card_tools.set_db_path(self.db_a)
        card_tools.init_db()
        graph_io.set_emission_repo_root(self.repo_a)

    def _use_repo_b(self):
        card_tools.set_db_path(self.db_b)
        card_tools.init_db()
        graph_io.set_emission_repo_root(self.repo_b)

    def _drain_a(self, url):
        cfg = ob_drain.DrainConfig(
            api_url=url, api_key=_KEY, repo_root=self.repo_a, timeout=3,
            backoff_initial=0.01, batch_size=500,
        )
        return ob_drain.Drainer(cfg).run_once()

    def test_card_in_a_becomes_visible_in_b(self):
        store = FakeStore()
        app = create_app(api_key=_KEY, store=store)
        with _serve(app) as url:
            client = Client(url, api_key=_KEY)

            # 1. Create a card on machine A (emits task.created to A's outbox).
            self._use_repo_a()
            created = card_tools.create_card(
                title="Cross-machine task", description="hello from A", priority="high"
            )
            gid = created["global_id"]
            self.assertIsNotNone(gid)

            # 2. Drain A -> central: the task is materialized in registry.tasks.
            self.assertEqual(self._drain_a(url), 0)
            central = client.get_tasks(repository_id=self.repo_a_id)["tasks"]
            self.assertEqual(len(central), 1)
            self.assertEqual(central[0]["global_id"], gid)

            # 3. Pull + apply DOWN into B's local card DB.
            self._use_repo_b()
            applied = task_inbox.poll_once(client, self.repo_b, self.repo_a_id)
            self.assertEqual(applied, 1)

            # 4. The card is now visible in B (title + status), keyed by global_id.
            b_cards = card_tools.list_cards()["cards"]
            self.assertEqual(len(b_cards), 1)
            b_card = b_cards[0]
            self.assertEqual(b_card["title"], "Cross-machine task")
            self.assertEqual(b_card["status"], "Created")
            self.assertEqual(b_card["global_id"], gid)
            b_card_id = b_card["card_id"]

            # A second poll with nothing new applies nothing (cursor caught up).
            self.assertEqual(task_inbox.poll_once(client, self.repo_b, self.repo_a_id), 0)

            # 5. Update the card on A -> drain -> re-pull: B updates IN PLACE.
            self._use_repo_a()
            card_tools.update_card(created["card_id"], status="In Progress")
            self.assertEqual(self._drain_a(url), 0)

            self._use_repo_b()
            self.assertEqual(task_inbox.poll_once(client, self.repo_b, self.repo_a_id), 1)
            b_cards = card_tools.list_cards()["cards"]
            self.assertEqual(len(b_cards), 1)                    # still ONE card
            self.assertEqual(b_cards[0]["card_id"], b_card_id)   # same local id
            self.assertEqual(b_cards[0]["status"], "In Progress")


if __name__ == "__main__":
    unittest.main()
