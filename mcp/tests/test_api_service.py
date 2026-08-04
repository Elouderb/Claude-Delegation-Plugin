"""Endpoint tests for the Agent OS /v1 API (card 8f84948b).

Flask test client over a FAKE in-memory store — no database, no network. Covers
bearer auth (401s + fail-closed), the machine/repository/session registration
upsert semantics (including the natural-key re-mint reconciliation), heartbeats,
the /v1/events/batch per-event outcome matrix (accepted / duplicate / rejected +
ops.ingest_errors recording + the 5xx transport path), and the read surface.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from contracts import AGENT, EVENT, MACHINE, REPOSITORY, SESSION, new_id, utc_now  # noqa: E402
from services.api.app import create_app  # noqa: E402
from services.api.store import FakeStore  # noqa: E402

_KEY = "test-api-key"
_AUTH = {"Authorization": f"Bearer {_KEY}"}


def _client(store=None, api_key=_KEY):
    store = store if store is not None else FakeStore()
    app = create_app(api_key=api_key, store=store)
    app.testing = True
    return app.test_client(), store


def _machine_payload(machine_id=None):
    return {
        "machine_id": machine_id or new_id(MACHINE),
        "name": "test-box",
        "os": "Linux",
        "created_at": utc_now().isoformat(),
    }


def _event(machine_id, repository_id, *, event_id=None, event_type="task.created"):
    return {
        "event_id": event_id or new_id(EVENT),
        "event_type": event_type,
        "occurred_at": utc_now().isoformat(),
        "machine_id": machine_id,
        "repository_id": repository_id,
        "task_id": "card1234",
        "payload": {"card_id": "card1234"},
    }


class TestAuth(unittest.TestCase):
    def test_health_is_unauthenticated(self):
        client, _ = _client()
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "ok")

    def test_missing_bearer_is_401(self):
        client, _ = _client()
        self.assertEqual(client.get("/v1/machines").status_code, 401)
        self.assertEqual(
            client.post("/v1/machines/register", json=_machine_payload()).status_code, 401
        )

    def test_wrong_and_blank_bearer_are_401(self):
        client, _ = _client()
        self.assertEqual(
            client.get("/v1/machines", headers={"Authorization": "Bearer nope"}).status_code, 401
        )
        self.assertEqual(
            client.get("/v1/machines", headers={"Authorization": "Bearer "}).status_code, 401
        )
        self.assertEqual(
            client.get("/v1/machines", headers={"Authorization": _KEY}).status_code, 401
        )

    def test_fail_closed_when_server_key_unset(self):
        # A server with no configured key authenticates nobody, even with a bearer.
        client, _ = _client(api_key="")
        self.assertEqual(client.get("/v1/machines", headers=_AUTH).status_code, 401)

    def test_unknown_path_still_requires_auth(self):
        client, _ = _client()
        self.assertEqual(client.get("/v1/nope").status_code, 401)
        self.assertEqual(client.get("/v1/nope", headers=_AUTH).status_code, 404)


class TestMachineRegister(unittest.TestCase):
    def test_register_then_reregister_is_update_not_duplicate(self):
        client, store = _client()
        payload = _machine_payload()
        r1 = client.post("/v1/machines/register", headers=_AUTH, json=payload)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.get_json()["status"], "created")
        payload["name"] = "renamed"
        r2 = client.post("/v1/machines/register", headers=_AUTH, json=payload)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.get_json()["status"], "updated")
        self.assertEqual(len(store.machines), 1)

    def test_invalid_payload_is_400(self):
        client, _ = _client()
        r = client.post("/v1/machines/register", headers=_AUTH, json={"name": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.get_json())

    def test_non_object_body_is_400(self):
        client, _ = _client()
        r = client.post("/v1/machines/register", headers=_AUTH, json=["not", "an", "object"])
        self.assertEqual(r.status_code, 400)


class TestRepositoryRegister(unittest.TestCase):
    def test_natural_key_remint_keeps_canonical_id(self):
        client, store = _client()
        machine_id = new_id(MACHINE)
        rid1, rid2 = new_id(REPOSITORY), new_id(REPOSITORY)
        root = "/home/dev/project"

        r1 = client.post("/v1/repositories/register", headers=_AUTH, json={
            "repository_id": rid1, "machine_id": machine_id, "repo_root": root,
            "created_at": utc_now().isoformat(),
        })
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.get_json()["repository_id"], rid1)
        self.assertFalse(r1.get_json()["remapped"])

        # Re-register the SAME checkout with a freshly-minted repository_id (the
        # .agent-os re-mint case) — must UPDATE the existing row and return the
        # ORIGINAL id as canonical, not insert a duplicate.
        r2 = client.post("/v1/repositories/register", headers=_AUTH, json={
            "repository_id": rid2, "machine_id": machine_id, "repo_root": root,
            "created_at": utc_now().isoformat(),
        })
        self.assertEqual(r2.status_code, 200)
        body = r2.get_json()
        self.assertEqual(body["repository_id"], rid1)  # canonical original id
        self.assertTrue(body["remapped"])
        self.assertEqual(body["status"], "updated")
        self.assertEqual(len(store.repositories), 1)

    def test_same_root_different_machine_is_distinct_row(self):
        client, store = _client()
        root = "/home/dev/project"
        client.post("/v1/repositories/register", headers=_AUTH, json={
            "repository_id": new_id(REPOSITORY), "machine_id": new_id(MACHINE),
            "repo_root": root, "created_at": utc_now().isoformat(),
        })
        client.post("/v1/repositories/register", headers=_AUTH, json={
            "repository_id": new_id(REPOSITORY), "machine_id": new_id(MACHINE),
            "repo_root": root, "created_at": utc_now().isoformat(),
        })
        self.assertEqual(len(store.repositories), 2)

    def test_missing_repo_root_is_400(self):
        client, _ = _client()
        r = client.post("/v1/repositories/register", headers=_AUTH, json={
            "repository_id": new_id(REPOSITORY), "machine_id": new_id(MACHINE),
            "created_at": utc_now().isoformat(),
        })
        self.assertEqual(r.status_code, 400)

    def test_same_repo_id_reminted_machine_refreshes_not_duplicates(self):
        # Mirror of the live re-mint refresh: the SAME repository_id re-registered
        # under a different (re-minted) machine + root updates the row's machine_id
        # instead of duplicating or looping (FakeStore parity with SqlStore).
        client, store = _client()
        repo_id = new_id(REPOSITORY)
        machine_a, machine_b = new_id(MACHINE), new_id(MACHINE)
        client.post("/v1/repositories/register", headers=_AUTH, json={
            "repository_id": repo_id, "machine_id": machine_a, "repo_root": "/r/a",
            "created_at": utc_now().isoformat(),
        })
        r2 = client.post("/v1/repositories/register", headers=_AUTH, json={
            "repository_id": repo_id, "machine_id": machine_b, "repo_root": "/r/b",
            "created_at": utc_now().isoformat(),
        })
        self.assertEqual(r2.get_json()["repository_id"], repo_id)
        self.assertFalse(r2.get_json()["remapped"])
        self.assertEqual(len(store.repositories), 1)
        self.assertEqual(store.repositories[repo_id]["machine_id"], machine_b)


class TestSessionRegisterAndHeartbeat(unittest.TestCase):
    def _register(self, client):
        machine_id = new_id(MACHINE)
        session_id = new_id(SESSION)
        r = client.post("/v1/sessions/register", headers=_AUTH, json={
            "session_id": session_id, "agent_id": new_id(AGENT), "agent_type": "implementer",
            "machine_id": machine_id, "status": "running", "capabilities": ["repo.read"],
            "started_at": utc_now().isoformat(),
        })
        return r, session_id

    def test_register_then_heartbeat(self):
        client, store = _client()
        r, session_id = self._register(client)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "created")
        hb = client.post(f"/v1/sessions/{session_id}/heartbeat", headers=_AUTH, json={})
        self.assertEqual(hb.status_code, 200)
        # Transport ack lives under 'result' so it never collides with a resource
        # 'status' field (finding: status-key overload).
        self.assertEqual(hb.get_json()["result"], "ok")
        self.assertNotIn("status", hb.get_json())
        self.assertIn("heartbeat_at", hb.get_json())

    def test_reregister_is_upsert(self):
        client, store = _client()
        r, session_id = self._register(client)
        r2 = client.post("/v1/sessions/register", headers=_AUTH, json={
            "session_id": session_id, "agent_id": new_id(AGENT), "agent_type": "implementer",
            "machine_id": new_id(MACHINE), "status": "finished",
            "started_at": utc_now().isoformat(),
        })
        self.assertEqual(r2.get_json()["status"], "updated")
        self.assertEqual(len(store.sessions), 1)

    def test_heartbeat_missing_session_is_404(self):
        client, _ = _client()
        r = client.post("/v1/sessions/sess_missing/heartbeat", headers=_AUTH, json={})
        self.assertEqual(r.status_code, 404)

    def test_heartbeat_invalid_status_is_400(self):
        client, _ = _client()
        _, session_id = self._register(client)
        r = client.post(f"/v1/sessions/{session_id}/heartbeat", headers=_AUTH,
                        json={"status": "bogus"})
        self.assertEqual(r.status_code, 400)


class TestEventsBatch(unittest.TestCase):
    def test_accepted_duplicate_rejected_matrix(self):
        client, store = _client()
        machine_id, repo_id = new_id(MACHINE), new_id(REPOSITORY)
        good = _event(machine_id, repo_id)
        bad = {"event_type": "task.created"}  # missing event_id/occurred_at/machine_id
        r = client.post("/v1/events/batch", headers=_AUTH,
                        json={"events": [good, dict(good), bad]})
        self.assertEqual(r.status_code, 200)
        outcomes = [item["outcome"] for item in r.get_json()["results"]]
        self.assertEqual(outcomes, ["accepted", "duplicate", "rejected"])
        # The rejected one is recorded for diagnosis.
        self.assertEqual(len(store.ingest_errors), 1)
        # Result ordering is index-aligned so the drain can map verdicts back.
        self.assertEqual([item["index"] for item in r.get_json()["results"]], [0, 1, 2])

    def test_empty_batch_ok(self):
        client, _ = _client()
        r = client.post("/v1/events/batch", headers=_AUTH, json={"events": []})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["results"], [])

    def test_events_not_a_list_is_400(self):
        client, _ = _client()
        self.assertEqual(
            client.post("/v1/events/batch", headers=_AUTH, json={"events": "x"}).status_code, 400
        )
        self.assertEqual(
            client.post("/v1/events/batch", headers=_AUTH, json=[]).status_code, 400
        )

    def test_db_error_is_500(self):
        # A store failure on a valid batch => 5xx (transport) so the drain holds
        # its cursor; the response must not leak the cause.
        client, _ = _client(store=FakeStore(raise_on_ingest=True))
        machine_id, repo_id = new_id(MACHINE), new_id(REPOSITORY)
        r = client.post("/v1/events/batch", headers=_AUTH,
                        json={"events": [_event(machine_id, repo_id)]})
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("simulated", str(r.get_json()))


class TestReadSurface(unittest.TestCase):
    def _seed(self, client):
        machine_id, repo_id = new_id(MACHINE), new_id(REPOSITORY)
        client.post("/v1/machines/register", headers=_AUTH, json=_machine_payload(machine_id))
        client.post("/v1/repositories/register", headers=_AUTH, json={
            "repository_id": repo_id, "machine_id": machine_id, "repo_root": "/r",
            "created_at": utc_now().isoformat(),
        })
        client.post("/v1/sessions/register", headers=_AUTH, json={
            "session_id": new_id(SESSION), "agent_id": new_id(AGENT), "agent_type": "impl",
            "machine_id": machine_id, "status": "running", "started_at": utc_now().isoformat(),
        })
        client.post("/v1/sessions/register", headers=_AUTH, json={
            "session_id": new_id(SESSION), "agent_id": new_id(AGENT), "agent_type": "impl",
            "machine_id": machine_id, "status": "finished", "started_at": utc_now().isoformat(),
        })
        client.post("/v1/events/batch", headers=_AUTH, json={"events": [
            _event(machine_id, repo_id, event_type="task.created"),
            _event(machine_id, repo_id, event_type="agent.started"),
        ]})
        return machine_id, repo_id

    def test_reads(self):
        client, _ = _client()
        machine_id, repo_id = self._seed(client)

        self.assertEqual(len(client.get("/v1/machines", headers=_AUTH).get_json()["machines"]), 1)
        self.assertEqual(
            len(client.get("/v1/repositories", headers=_AUTH).get_json()["repositories"]), 1
        )
        # active=1 filters finished sessions out (1 running of 2 total).
        self.assertEqual(
            len(client.get("/v1/sessions?active=1", headers=_AUTH).get_json()["sessions"]), 1
        )
        self.assertEqual(
            len(client.get("/v1/sessions", headers=_AUTH).get_json()["sessions"]), 2
        )
        recent = client.get("/v1/events/recent", headers=_AUTH).get_json()["events"]
        self.assertEqual(len(recent), 2)
        filtered = client.get(
            f"/v1/events/recent?event_type=agent.started&repository_id={repo_id}",
            headers=_AUTH,
        ).get_json()["events"]
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["event_type"], "agent.started")


class TestErrorEnvelope(unittest.TestCase):
    def test_error_shape_is_code_and_message(self):
        client, _ = _client()
        # 401 (auth) and 400 (bad payload) both carry {"error": {"code", "message"}}.
        for resp in (
            client.get("/v1/machines"),
            client.post("/v1/machines/register", headers=_AUTH, json={"name": "x"}),
        ):
            body = resp.get_json()
            self.assertIn("error", body)
            self.assertIsInstance(body["error"], dict)
            self.assertIn("code", body["error"])
            self.assertIn("message", body["error"])
        self.assertEqual(
            client.get("/v1/machines").get_json()["error"]["code"], "unauthorized"
        )


class TestApiKeyList(unittest.TestCase):
    def test_comma_separated_keys_all_accepted(self):
        # A rotation window: old + new key both authenticate; an unlisted one 401s.
        client, _ = _client(api_key="oldkey, newkey")
        self.assertEqual(
            client.get("/v1/machines", headers={"Authorization": "Bearer oldkey"}).status_code, 200
        )
        self.assertEqual(
            client.get("/v1/machines", headers={"Authorization": "Bearer newkey"}).status_code, 200
        )
        self.assertEqual(
            client.get("/v1/machines", headers={"Authorization": "Bearer other"}).status_code, 401
        )


class TestBatchCap(unittest.TestCase):
    def test_oversize_batch_is_413(self):
        client, store = _client()
        machine_id, repo_id = new_id(MACHINE), new_id(REPOSITORY)
        events = [_event(machine_id, repo_id) for _ in range(501)]
        r = client.post("/v1/events/batch", headers=_AUTH, json={"events": events})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.get_json()["error"]["code"], "batch_too_large")
        self.assertEqual(len(store.events), 0)  # nothing ingested


def _agent_event(machine_id, session_id, *, event_type, agent_id=None, agent_type="implementer"):
    return {
        "event_id": new_id(EVENT), "event_type": event_type, "occurred_at": utc_now().isoformat(),
        "machine_id": machine_id, "agent_id": agent_id or new_id(AGENT), "session_id": session_id,
        "payload": {"agent_type": agent_type},
    }


class TestSessionMaterialization(unittest.TestCase):
    def test_agent_events_materialize_and_close_sessions(self):
        client, _ = _client()
        machine_id = new_id(MACHINE)
        s_running, s_done = new_id(SESSION), new_id(SESSION)
        client.post("/v1/events/batch", headers=_AUTH, json={"events": [
            _agent_event(machine_id, s_running, event_type="agent.started"),
            _agent_event(machine_id, s_done, event_type="agent.started"),
            _agent_event(machine_id, s_done, event_type="agent.finished"),
        ]})
        # Both sessions exist; only the still-running one is 'active'.
        active = client.get("/v1/sessions?active=1", headers=_AUTH).get_json()["sessions"]
        self.assertEqual([s["session_id"] for s in active], [s_running])
        self.assertEqual(active[0]["status"], "running")
        self.assertEqual(active[0]["agent_type"], "implementer")
        all_sessions = {s["session_id"]: s for s in
                        client.get("/v1/sessions", headers=_AUTH).get_json()["sessions"]}
        self.assertEqual(all_sessions[s_done]["status"], "finished")
        self.assertIsNotNone(all_sessions[s_done]["ended_at"])


class TestRecentPagination(unittest.TestCase):
    def test_ordering_seq_received_at_and_keyset(self):
        client, _ = _client()
        machine_id, repo_id = new_id(MACHINE), new_id(REPOSITORY)
        client.post("/v1/events/batch", headers=_AUTH, json={"events": [
            _event(machine_id, repo_id) for _ in range(5)
        ]})
        page1 = client.get("/v1/events/recent?limit=2", headers=_AUTH).get_json()
        seqs = [e["ingest_seq"] for e in page1["events"]]
        self.assertEqual(seqs, sorted(seqs, reverse=True))  # newest-first by ingest_seq
        self.assertTrue(all("received_at" in e for e in page1["events"]))
        self.assertEqual(page1["next_after"], seqs[-1])
        # Keyset continuation returns strictly-older events, no overlap.
        page2 = client.get(
            f"/v1/events/recent?limit=2&after_seq={page1['next_after']}", headers=_AUTH
        ).get_json()
        self.assertTrue(all(e["ingest_seq"] < page1["next_after"] for e in page2["events"]))


class TestFakeStoreFidelity(unittest.TestCase):
    def test_reason_is_truncated_like_sql_store(self):
        # SqlStore truncates ops.ingest_errors.reason to 1000 chars; the fake must too
        # so a test built on the fake can't miss a truncation regression on the real path.
        from services.api.store import FakeStore, PreparedEvent
        store = FakeStore()
        store.ingest_events([PreparedEvent(envelope=None, raw="x", error="e" * 5000,
                                           source_machine_id=None)])
        self.assertEqual(len(store.ingest_errors[0]["reason"]), 1000)


class TestSyncCursors(unittest.TestCase):
    """GET/PUT /v1/sync/{client_id} (card 93baf05b, deferred from 8f84948b)."""

    def test_unknown_client_returns_nulls_not_404(self):
        client, _ = _client()
        resp = client.get("/v1/sync/never-seen", headers=_AUTH)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["client_id"], "never-seen")
        self.assertIsNone(body["last_seq"])
        self.assertIsNone(body["last_event_id"])
        self.assertIsNone(body["updated_at"])

    def test_put_then_get_round_trips(self):
        client, _ = _client()
        put = client.put(
            "/v1/sync/m3-standin", headers=_AUTH,
            json={"last_seq": 42, "last_event_id": "evt_abc"},
        )
        self.assertEqual(put.status_code, 200, put.get_data(as_text=True))
        body = put.get_json()
        self.assertEqual(body["last_seq"], 42)
        self.assertEqual(body["last_event_id"], "evt_abc")
        self.assertIsNotNone(body["updated_at"])

        get = client.get("/v1/sync/m3-standin", headers=_AUTH)
        self.assertEqual(get.get_json()["last_seq"], 42)

    def test_put_is_upsert_not_duplicate(self):
        client, _ = _client()
        client.put("/v1/sync/m3-standin", headers=_AUTH, json={"last_seq": 1, "last_event_id": "a"})
        second = client.put(
            "/v1/sync/m3-standin", headers=_AUTH, json={"last_seq": 2, "last_event_id": "b"}
        )
        self.assertEqual(second.get_json()["last_seq"], 2)
        self.assertEqual(client.get("/v1/sync/m3-standin", headers=_AUTH).get_json()["last_seq"], 2)

    def test_put_accepts_null_position(self):
        # A client explicitly resetting its position (e.g. --client-id reused
        # for a fresh follow) must be able to clear last_seq/last_event_id.
        client, _ = _client()
        client.put("/v1/sync/x", headers=_AUTH, json={"last_seq": 5, "last_event_id": "e"})
        resp = client.put("/v1/sync/x", headers=_AUTH, json={"last_seq": None, "last_event_id": None})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.get_json()["last_seq"])

    def test_invalid_last_seq_is_400(self):
        client, _ = _client()
        resp = client.put(
            "/v1/sync/x", headers=_AUTH, json={"last_seq": "not-a-number", "last_event_id": None}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"]["code"], "invalid_payload")

    def test_negative_last_seq_is_400(self):
        # A negative last_seq would otherwise be accepted and, on the client's
        # next --follow tick, be treated as a valid-but-ancient cursor — walking
        # (and re-displaying) the entire event history (code review, card
        # 93baf05b, Major finding #3).
        client, _ = _client()
        resp = client.put(
            "/v1/sync/x", headers=_AUTH, json={"last_seq": -1, "last_event_id": None}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"]["code"], "invalid_payload")
        # Nothing was persisted from the rejected PUT.
        self.assertIsNone(client.get("/v1/sync/x", headers=_AUTH).get_json()["last_seq"])

    def test_overflow_last_seq_is_400(self):
        # One past SQL Server's BIGINT max — must be rejected as a clean 400
        # rather than reaching the driver and surfacing as a sanitized 500.
        client, _ = _client()
        resp = client.put(
            "/v1/sync/x", headers=_AUTH,
            json={"last_seq": 9223372036854775808, "last_event_id": None},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"]["code"], "invalid_payload")

    def test_last_seq_boundary_values_accepted(self):
        # 0 and the exact BIGINT max are valid, not off-by-one rejected.
        client, _ = _client()
        low = client.put("/v1/sync/lo", headers=_AUTH, json={"last_seq": 0, "last_event_id": None})
        self.assertEqual(low.status_code, 200)
        self.assertEqual(low.get_json()["last_seq"], 0)

        high = client.put(
            "/v1/sync/hi", headers=_AUTH,
            json={"last_seq": 9223372036854775807, "last_event_id": None},
        )
        self.assertEqual(high.status_code, 200)
        self.assertEqual(high.get_json()["last_seq"], 9223372036854775807)

    def test_non_object_body_is_400(self):
        client, _ = _client()
        resp = client.put(
            "/v1/sync/x", headers=_AUTH, data="[]", content_type="application/json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_oversized_client_id_is_400(self):
        client, _ = _client()
        resp = client.get(f"/v1/sync/{'x' * 65}", headers=_AUTH)
        self.assertEqual(resp.status_code, 400)

    def test_requires_auth(self):
        client, _ = _client()
        resp = client.get("/v1/sync/m3-standin")
        self.assertEqual(resp.status_code, 401)
        resp = client.put("/v1/sync/m3-standin", json={"last_seq": 1, "last_event_id": None})
        self.assertEqual(resp.status_code, 401)

    def test_two_clients_do_not_share_a_cursor(self):
        client, _ = _client()
        client.put("/v1/sync/a", headers=_AUTH, json={"last_seq": 10, "last_event_id": None})
        client.put("/v1/sync/b", headers=_AUTH, json={"last_seq": 20, "last_event_id": None})
        self.assertEqual(client.get("/v1/sync/a", headers=_AUTH).get_json()["last_seq"], 10)
        self.assertEqual(client.get("/v1/sync/b", headers=_AUTH).get_json()["last_seq"], 20)


if __name__ == "__main__":
    unittest.main()
