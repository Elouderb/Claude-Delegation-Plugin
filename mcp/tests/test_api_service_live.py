"""LIVE integration test for the Agent OS /v1 API against the real central DB.

Gated on ``AGENT_OS_CENTRAL_DB_CONNECTION_STRING`` (skips cleanly in CI — the db_*
suite precedent). Drives the real :class:`~services.api.store.SqlStore` through the
Flask test client end to end: register machine -> repository -> session ->
heartbeat, POST an event batch, read it back via ``GET /v1/events/recent``, and
verify the natural-key re-mint reconciliation against the live schema.

Every id is freshly minted per run and every row this test writes
(registry.machines/repositories/agent_sessions + ops.events) is DELETEd in
``tearDown`` (FK-safe order), so the shared ``agent_os_memory`` schema is left
exactly as found — no residual test data. Never prints/asserts the connection
string; only ids, counts, and outcomes.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from contracts import AGENT, EVENT, MACHINE, REPOSITORY, SESSION, new_id, utc_now  # noqa: E402
from database import migrate  # noqa: E402
from services.api.app import create_app  # noqa: E402
from services.api.store import SqlStore  # noqa: E402

# DOUBLE-gated: a connection string AND an explicit opt-in are BOTH required.
# setUpClass calls migrate.apply_pending against the REAL DB, so presence of a
# connection string alone is unsafe (a plain full-suite run would silently migrate
# production). Opt in deliberately with AGENT_OS_RUN_LIVE_DB_TESTS=1.
_HAS_CENTRAL_DB = bool(os.environ.get(migrate.ENV_VAR)) and (
    os.environ.get("AGENT_OS_RUN_LIVE_DB_TESTS") == "1"
)
_KEY = "live-test-key"
_AUTH = {"Authorization": f"Bearer {_KEY}"}


@unittest.skipUnless(
    _HAS_CENTRAL_DB,
    f"live API integration test requires {migrate.ENV_VAR} + AGENT_OS_RUN_LIVE_DB_TESTS=1",
)
class TestLiveApiService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Ensure the schema exists (idempotent no-op if already applied).
        conn = migrate.connect(timeout=30)
        try:
            migrate.apply_pending(migrate.DEFAULT_MIGRATIONS_DIR, conn)
        finally:
            conn.close()
        cls.app = create_app(api_key=_KEY, store=SqlStore(migrate.connect))
        cls.app.testing = True

    def setUp(self):
        self.client = self.app.test_client()
        self._machines: list[str] = []
        self._repositories: list[str] = []
        self._sessions: list[str] = []
        self._events: list[str] = []

    def tearDown(self):
        conn = migrate.connect(timeout=30)
        try:
            cur = conn.cursor()
            for event_id in self._events:
                cur.execute("DELETE FROM ops.events WHERE event_id = ?", event_id)
            for session_id in self._sessions:
                cur.execute("DELETE FROM registry.agent_sessions WHERE session_id = ?", session_id)
            for repository_id in self._repositories:
                cur.execute("DELETE FROM registry.repositories WHERE repository_id = ?", repository_id)
            for machine_id in self._machines:
                cur.execute("DELETE FROM registry.machines WHERE machine_id = ?", machine_id)
            conn.commit()
        finally:
            conn.close()

    def _register_machine(self):
        machine_id = new_id(MACHINE)
        self._machines.append(machine_id)
        r = self.client.post("/v1/machines/register", headers=_AUTH, json={
            "machine_id": machine_id, "name": "live-test", "os": "Linux",
            "created_at": utc_now().isoformat(),
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return machine_id

    def _register_repo(self, machine_id, repo_root):
        repository_id = new_id(REPOSITORY)
        self._repositories.append(repository_id)
        r = self.client.post("/v1/repositories/register", headers=_AUTH, json={
            "repository_id": repository_id, "machine_id": machine_id, "repo_root": repo_root,
            "canonical_remote": "github.com/live/test", "created_at": utc_now().isoformat(),
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return repository_id, r.get_json()

    def test_full_registration_and_event_round_trip(self):
        machine_id = self._register_machine()
        repository_id, _ = self._register_repo(machine_id, "/live/test/full-round-trip")

        session_id = new_id(SESSION)
        self._sessions.append(session_id)
        r = self.client.post("/v1/sessions/register", headers=_AUTH, json={
            "session_id": session_id, "agent_id": new_id(AGENT), "agent_type": "sync-drain",
            "machine_id": machine_id, "repository_id": repository_id, "status": "running",
            "capabilities": ["repo.read"], "started_at": utc_now().isoformat(),
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

        hb = self.client.post(f"/v1/sessions/{session_id}/heartbeat", headers=_AUTH, json={})
        self.assertEqual(hb.status_code, 200)

        event_id = new_id(EVENT)
        self._events.append(event_id)
        # Send twice: first accepted, second duplicate (idempotent ingest).
        event = {
            "event_id": event_id, "event_type": "task.created", "occurred_at": utc_now().isoformat(),
            "machine_id": machine_id, "repository_id": repository_id, "task_id": "livecard",
            "payload": {"card_id": "livecard"},
        }
        batch = self.client.post("/v1/events/batch", headers=_AUTH,
                                 json={"events": [event, dict(event)]})
        self.assertEqual(batch.status_code, 200, batch.get_data(as_text=True))
        outcomes = [row["outcome"] for row in batch.get_json()["results"]]
        self.assertEqual(outcomes, ["accepted", "duplicate"])

        recent = self.client.get(
            f"/v1/events/recent?event_type=task.created&repository_id={repository_id}",
            headers=_AUTH,
        )
        self.assertEqual(recent.status_code, 200)
        ids = [e["event_id"] for e in recent.get_json()["events"]]
        self.assertIn(event_id, ids)

        # The machine/repo/session are queryable on the read surface.
        machines = self.client.get("/v1/machines", headers=_AUTH).get_json()["machines"]
        self.assertIn(machine_id, [m["machine_id"] for m in machines])

    def test_repository_remint_returns_canonical_id_live(self):
        machine_id = self._register_machine()
        root = "/live/test/remint"
        first_id, _ = self._register_repo(machine_id, root)

        # Re-register the SAME checkout with a new id — must return the original.
        remint_id = new_id(REPOSITORY)
        self._repositories.append(remint_id)  # tracked for cleanup even though not inserted
        r = self.client.post("/v1/repositories/register", headers=_AUTH, json={
            "repository_id": remint_id, "machine_id": machine_id, "repo_root": root,
            "created_at": utc_now().isoformat(),
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertEqual(body["repository_id"], first_id)
        self.assertTrue(body["remapped"])
        self.assertEqual(body["status"], "updated")

    def test_case_differing_roots_are_two_repositories_live(self):
        # Finding #4: the natural-key lookup must compare on the byte-exact
        # repo_root_hash (like the UNIQUE index), NOT a plain repo_root string
        # equality that collapses two checkouts under this DB's case-insensitive
        # collation into one identity.
        machine_id = self._register_machine()
        upper_id, _ = self._register_repo(machine_id, "/Live/Test/CaseFix")
        lower_id, body = self._register_repo(machine_id, "/live/test/casefix")
        self.assertNotEqual(upper_id, lower_id)
        self.assertEqual(body["repository_id"], lower_id)  # a NEW row, not a remap
        self.assertFalse(body["remapped"])
        self.assertEqual(body["status"], "created")

    def test_reregister_under_reminted_machine_refreshes_machine_id_live(self):
        # Finding #3 (re-mint register loop): the same repository_id re-registered
        # under a DIFFERENT (re-minted) machine must refresh the row's machine_id
        # instead of failing the machine FK forever. Drive SqlStore directly (the
        # app would surface a genuine failure as 500) with the machine pre-registered.
        store = SqlStore(migrate.connect)
        machine_a, machine_b = self._register_machine(), self._register_machine()
        repo_id = new_id(REPOSITORY)
        self._repositories.append(repo_id)
        root = "/live/test/remint-machine"
        first = store.upsert_repository(
            repository_id=repo_id, machine_id=machine_a, repo_root=root,
            canonical_remote=None, project_id=None, created_at=utc_now(),
        )
        self.assertTrue(first["created"])
        # Same repository_id, different machine, different root (so the natural key
        # (machine_b, new_root) MISSES) -> forces the except-branch PK-refresh path.
        again = store.upsert_repository(
            repository_id=repo_id, machine_id=machine_b, repo_root="/live/test/remint-machine-b",
            canonical_remote=None, project_id=None, created_at=utc_now(),
        )
        self.assertEqual(again["repository_id"], repo_id)
        self.assertFalse(again["created"])
        repos = {r["repository_id"]: r for r in
                 self.client.get("/v1/repositories", headers=_AUTH).get_json()["repositories"]}
        self.assertEqual(repos[repo_id]["machine_id"], machine_b)  # refreshed

    def test_unregistered_machine_fk_miss_reraises_live(self):
        # A genuine FK failure (repository for a machine that was never registered)
        # must NOT be silently reconciled — it re-raises so the app returns 500.
        import pyodbc
        store = SqlStore(migrate.connect)
        with self.assertRaises(pyodbc.IntegrityError):
            store.upsert_repository(
                repository_id=new_id(REPOSITORY), machine_id=new_id(MACHINE),
                repo_root="/live/test/fk-miss", canonical_remote=None, project_id=None,
                created_at=utc_now(),
            )

    def test_agent_events_materialize_sessions_live(self):
        # §17 "list active agents": agent.started materializes a running session,
        # agent.finished closes it — queryable on the read surface.
        machine_id = self._register_machine()
        s_run, s_done = new_id(SESSION), new_id(SESSION)
        self._sessions.extend([s_run, s_done])
        events = []
        for sid, etype in ((s_run, "agent.started"), (s_done, "agent.started"),
                           (s_done, "agent.finished")):
            eid = new_id(EVENT)
            self._events.append(eid)
            events.append({
                "event_id": eid, "event_type": etype, "occurred_at": utc_now().isoformat(),
                "machine_id": machine_id, "agent_id": new_id(AGENT), "session_id": sid,
                "payload": {"agent_type": "implementer"},
            })
        r = self.client.post("/v1/events/batch", headers=_AUTH, json={"events": events})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        active = {s["session_id"] for s in
                  self.client.get("/v1/sessions?active=1", headers=_AUTH).get_json()["sessions"]}
        self.assertIn(s_run, active)
        self.assertNotIn(s_done, active)  # finished -> not active
        all_s = {s["session_id"]: s for s in
                 self.client.get("/v1/sessions", headers=_AUTH).get_json()["sessions"]}
        self.assertEqual(all_s[s_done]["status"], "finished")
        self.assertIsNotNone(all_s[s_done]["ended_at"])

    def test_recent_events_keyset_pagination_live(self):
        machine_id = self._register_machine()
        repository_id, _ = self._register_repo(machine_id, "/live/test/keyset")
        events = []
        for _ in range(4):
            eid = new_id(EVENT)
            self._events.append(eid)
            events.append({
                "event_id": eid, "event_type": "task.created", "occurred_at": utc_now().isoformat(),
                "machine_id": machine_id, "repository_id": repository_id, "task_id": "kc",
                "payload": {},
            })
        self.client.post("/v1/events/batch", headers=_AUTH, json={"events": events})
        p1 = self.client.get(
            f"/v1/events/recent?limit=2&repository_id={repository_id}", headers=_AUTH
        ).get_json()
        seqs = [e["ingest_seq"] for e in p1["events"]]
        self.assertEqual(len(seqs), 2)
        self.assertEqual(seqs, sorted(seqs, reverse=True))
        self.assertEqual(p1["next_after"], seqs[-1])
        self.assertTrue(all("received_at" in e for e in p1["events"]))
        p2 = self.client.get(
            f"/v1/events/recent?limit=2&repository_id={repository_id}&after_seq={p1['next_after']}",
            headers=_AUTH,
        ).get_json()
        self.assertTrue(all(e["ingest_seq"] < p1["next_after"] for e in p2["events"]))
        # No overlap between the two pages.
        self.assertFalse(
            {e["event_id"] for e in p1["events"]} & {e["event_id"] for e in p2["events"]}
        )

    def test_sync_cursor_round_trip_live(self):
        # Card 93baf05b's sync-cursor decision, against the real ops.sync_cursors
        # table (migration 005's last_seq column) rather than the FakeStore.
        client_id = new_id(SESSION)  # any unique NVARCHAR(64)-safe string works
        try:
            unseen = self.client.get(f"/v1/sync/{client_id}", headers=_AUTH)
            self.assertEqual(unseen.status_code, 200)
            self.assertIsNone(unseen.get_json()["last_seq"])

            put = self.client.put(
                f"/v1/sync/{client_id}", headers=_AUTH,
                json={"last_seq": 12345, "last_event_id": "evt_livecursor"},
            )
            self.assertEqual(put.status_code, 200, put.get_data(as_text=True))
            self.assertEqual(put.get_json()["last_seq"], 12345)

            get = self.client.get(f"/v1/sync/{client_id}", headers=_AUTH)
            body = get.get_json()
            self.assertEqual(body["last_seq"], 12345)
            self.assertEqual(body["last_event_id"], "evt_livecursor")
            self.assertIsNotNone(body["updated_at"])

            # Upsert (re-PUT), not a duplicate row.
            put2 = self.client.put(
                f"/v1/sync/{client_id}", headers=_AUTH,
                json={"last_seq": 12399, "last_event_id": "evt_livecursor2"},
            )
            self.assertEqual(put2.get_json()["last_seq"], 12399)
        finally:
            conn = migrate.connect(timeout=30)
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM ops.sync_cursors WHERE client_id = ?", client_id)
                conn.commit()
            finally:
                conn.close()

    def test_sync_cursor_last_seq_boundary_live(self):
        # Code review, card 93baf05b, Major finding #3: an out-of-range last_seq
        # must be rejected as a clean 400 by the app layer before it ever reaches
        # the real BIGINT column, and the exact BIGINT max must round-trip through
        # it without truncation/overflow at the driver.
        client_id = new_id(SESSION)
        try:
            overflow = self.client.put(
                f"/v1/sync/{client_id}", headers=_AUTH,
                json={"last_seq": 9223372036854775808, "last_event_id": None},
            )
            self.assertEqual(overflow.status_code, 400)
            self.assertEqual(overflow.get_json()["error"]["code"], "invalid_payload")
            # The rejected PUT never reached the store - still unseen.
            unseen = self.client.get(f"/v1/sync/{client_id}", headers=_AUTH)
            self.assertIsNone(unseen.get_json()["last_seq"])

            negative = self.client.put(
                f"/v1/sync/{client_id}", headers=_AUTH,
                json={"last_seq": -1, "last_event_id": None},
            )
            self.assertEqual(negative.status_code, 400)

            boundary = self.client.put(
                f"/v1/sync/{client_id}", headers=_AUTH,
                json={"last_seq": 9223372036854775807, "last_event_id": "evt_boundary"},
            )
            self.assertEqual(boundary.status_code, 200, boundary.get_data(as_text=True))
            self.assertEqual(boundary.get_json()["last_seq"], 9223372036854775807)

            get = self.client.get(f"/v1/sync/{client_id}", headers=_AUTH)
            self.assertEqual(get.get_json()["last_seq"], 9223372036854775807)
        finally:
            conn = migrate.connect(timeout=30)
            try:
                cur = conn.cursor()
                cur.execute("DELETE FROM ops.sync_cursors WHERE client_id = ?", client_id)
                conn.commit()
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
