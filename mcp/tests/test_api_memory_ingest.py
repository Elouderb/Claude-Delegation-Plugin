"""Endpoint tests for POST /v1/memory/ingest (card 39914456, epic 6fead93b).

Hermetic: a Flask test client over a FAKE in-memory memory store
(:class:`FakeMemoryStore`) and a DETERMINISTIC fake embedder — NO SQL Server, NO
real model, NO network — exactly mirroring test_api_service.py's _client()/
FakeStore pattern. Covers: new prose doc, re-ingest identical → unchanged (dedup),
re-ingest changed under same identity → replaced, content-hash collision across
identities → reconciled (not duplicated), source_type validation (news requires
published_at), auth (401 without bearer), provenance (source_machine_id = caller's
machine, admin body override), and the availability gate (→ 503 when the memory
subsystem is unavailable).
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from contracts import MACHINE, new_id, utc_now  # noqa: E402
from memory_core.availability import MemoryUnavailable  # noqa: E402
from services.api.app import create_app  # noqa: E402
from services.api.memory_store import FakeMemoryStore  # noqa: E402
from services.api.store import FakeStore  # noqa: E402

_KEY = "test-api-key"
_AUTH = {"Authorization": f"Bearer {_KEY}"}
_DIM = 8

PROSE = "A small domesticated feline that purrs when it is content."
PROSE2 = "An entirely different sentence about volcanoes and molten lava."


class FakeEmbedder:
    """Deterministic embedder (dim _DIM): text -> a fixed hashed unit-ish vector.

    Pure Python (hashlib only) — no model, no torch — so ingest embeds without
    any heavy dependency, the memory-store test double's whole point.
    """

    def __init__(self, dim: int = _DIM):
        self._dim = dim
        self.model_name = "fake-embedder-v1"

    @property
    def dim(self) -> int:
        return self._dim

    def _vec(self, text: str):
        digest = hashlib.sha256((text or "").encode("utf-8")).digest()
        return [digest[i % len(digest)] / 255.0 for i in range(self._dim)]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


def _raise_unavailable():
    raise MemoryUnavailable("memory subsystem unavailable (test)")


def _mem_client(*, memory_store=None, memory_embedder=None, api_key=_KEY, store=None):
    store = store if store is not None else FakeStore()
    memory_store = memory_store if memory_store is not None else FakeMemoryStore(embedding_dim=_DIM)
    if memory_embedder is None:
        memory_embedder = lambda: FakeEmbedder(_DIM)  # noqa: E731
    app = create_app(
        api_key=api_key, store=store, memory_store=memory_store,
        memory_embedder=memory_embedder,
    )
    app.testing = True
    return app.test_client(), memory_store, store


def _ingest(client, headers=_AUTH, **body):
    return client.post("/v1/memory/ingest", headers=headers, json=body)


def _conversation(user_text, assistant_text, *, conversation_id=None):
    """Build one ChatGPT-export conversation dict. Omit ``conversation_id`` for an
    ID-LESS conversation (the anonymous-ingest blocker case)."""
    conv = {
        "title": "T",
        "current_node": "m2",
        "mapping": {
            "root": {"id": "root", "parent": None, "children": ["m1"], "message": None},
            "m1": {
                "id": "m1", "parent": "root", "children": ["m2"],
                "message": {"id": "m1", "author": {"role": "user"},
                            "content": {"parts": [user_text]}},
            },
            "m2": {
                "id": "m2", "parent": "m1", "children": [],
                "message": {"id": "m2", "author": {"role": "assistant"},
                            "content": {"parts": [assistant_text]}},
            },
        },
    }
    if conversation_id is not None:
        conv["conversation_id"] = conversation_id
    return conv


def _chatgpt_export(*conversations):
    return json.dumps(list(conversations))


def _machine_payload(machine_id):
    return {
        "machine_id": machine_id,
        "name": "test-box",
        "os": "Linux",
        "created_at": utc_now().isoformat(),
    }


def _issue_machine_key(client, store):
    """Register a machine (admin) + issue a per-machine credential; return (machine_id, bearer)."""
    machine_id = new_id(MACHINE)
    client.post("/v1/machines/register", headers=_AUTH, json=_machine_payload(machine_id))
    resp = client.post(f"/v1/machines/{machine_id}/credentials", headers=_AUTH, json={})
    key = resp.get_json()["key"]
    return machine_id, {"Authorization": f"Bearer {key}"}


class TestIngestBasic(unittest.TestCase):
    def test_new_prose_document_is_created_with_chunks(self):
        client, mem, _ = _mem_client()
        resp = _ingest(client, content=PROSE, source_path="/corpus/cats.md")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["available"])
        self.assertEqual(body["operation"], "memory_ingest")
        self.assertEqual(body["documents_new"], 1)
        self.assertEqual(body["documents_detected"], 1)
        self.assertGreaterEqual(body["chunks_written"], 1)
        self.assertEqual(len(mem.documents), 1)
        self.assertEqual(len(mem.chunks), body["chunks_written"])
        # Vectors are stored per chunk (fake vector storage — Slice 2 read path).
        self.assertEqual(len(mem.vectors), body["chunks_written"])
        stats = mem.stats()
        self.assertEqual(stats["total_documents"], 1)
        self.assertEqual(stats["by_source_type"]["document"]["documents"], 1)

    def test_chunk_id_and_model_recorded(self):
        client, mem, _ = _mem_client()
        _ingest(client, content=PROSE, source_path="/corpus/cats.md")
        (cid, chunk), = list(mem.chunks.items())
        self.assertEqual(cid, f"mem:{chunk['document_id']}:{chunk['seq']}")
        self.assertEqual(chunk["embedding_model"], "fake-embedder-v1")
        self.assertEqual(chunk["embedding_dim"], _DIM)


class TestDedupAndReplace(unittest.TestCase):
    def test_reingest_identical_is_unchanged_noop(self):
        client, mem, _ = _mem_client()
        first = _ingest(client, content=PROSE, source_path="/corpus/cats.md").get_json()
        self.assertEqual(first["documents_new"], 1)
        chunks_after_first = len(mem.chunks)
        second = _ingest(client, content=PROSE, source_path="/corpus/cats.md").get_json()
        self.assertEqual(second["documents_new"], 0)
        self.assertEqual(second["documents_unchanged"], 1)
        self.assertEqual(second["chunks_written"], 0)
        # No new document, no new chunks written.
        self.assertEqual(len(mem.documents), 1)
        self.assertEqual(len(mem.chunks), chunks_after_first)

    def test_changed_content_same_identity_replaces(self):
        client, mem, _ = _mem_client()
        _ingest(client, content=PROSE, source_path="/corpus/cats.md")
        doc_id_before, = list(mem.documents.keys())
        second = _ingest(client, content=PROSE2, source_path="/corpus/cats.md").get_json()
        self.assertEqual(second["documents_replaced"], 1)
        self.assertEqual(second["documents_new"], 0)
        # Same identity (derived from source_path) -> exactly one document, updated.
        self.assertEqual(len(mem.documents), 1)
        self.assertEqual(list(mem.documents.keys()), [doc_id_before])
        texts = " ".join(c["chunk_text"] for c in mem.chunks.values())
        self.assertIn("volcanoes", texts)
        self.assertNotIn("domesticated feline", texts)

    def test_content_hash_collision_across_identities_is_reconciled(self):
        client, mem, _ = _mem_client()
        first = _ingest(client, content=PROSE, source_path="/corpus/a.md").get_json()
        self.assertEqual(first["documents_new"], 1)
        # SAME content, DIFFERENT identity key -> a different document_id would be
        # minted, but the global content_hash UNIQUE reconciles to the existing row.
        second = _ingest(client, content=PROSE, source_path="/corpus/b.md").get_json()
        self.assertEqual(second["documents_new"], 0)
        self.assertEqual(second["documents_unchanged"], 1)
        # Not duplicated: still exactly one document in the corpus.
        self.assertEqual(len(mem.documents), 1)
        self.assertEqual(mem.stats()["total_documents"], 1)

    def test_same_content_changed_metadata_updates_without_reembedding(self):
        # Same content + same identity (source_path) but CHANGED metadata
        # (published_at) lands in the metadata-only bucket — the local store's
        # 3-way classify — not replaced/unchanged, and does not re-embed.
        client, mem, _ = _mem_client()
        first = _ingest(
            client, content=PROSE, source_path="/corpus/cats.md",
            published_at="2024-01-15",
        ).get_json()
        self.assertEqual(first["documents_new"], 1)
        chunks_after_first = len(mem.chunks)
        second = _ingest(
            client, content=PROSE, source_path="/corpus/cats.md",
            published_at="2024-06-20",
        ).get_json()
        self.assertEqual(second["documents_metadata_updated"], 1)
        self.assertEqual(second["documents_new"], 0)
        self.assertEqual(second["documents_replaced"], 0)
        self.assertEqual(second["documents_unchanged"], 0)
        self.assertEqual(second["chunks_written"], 0)
        # Metadata was updated in place; chunks were NOT rewritten.
        self.assertEqual(len(mem.chunks), chunks_after_first)
        doc, = list(mem.documents.values())
        self.assertEqual(doc["published_at"], "2024-06-20")

    def test_reconcile_within_a_single_ingest_via_no_path_identities(self):
        # Two ingests with NO source_path each mint a fresh mem_<ULID> identity;
        # identical content still reconciles by content hash on the second.
        client, mem, _ = _mem_client()
        _ingest(client, content=PROSE).get_json()
        second = _ingest(client, content=PROSE).get_json()
        self.assertEqual(second["documents_new"], 0)
        self.assertEqual(second["documents_unchanged"], 1)
        self.assertEqual(len(mem.documents), 1)


class TestValidation(unittest.TestCase):
    def test_missing_content_is_400(self):
        client, _, _ = _mem_client()
        self.assertEqual(_ingest(client, source_path="/x").status_code, 400)
        self.assertEqual(_ingest(client, content="   ").status_code, 400)

    def test_news_requires_published_at_is_400(self):
        client, mem, _ = _mem_client()
        resp = _ingest(client, content=PROSE, source_type="news", source_path="/n.md")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("published_at", resp.get_json()["error"]["message"])
        self.assertEqual(len(mem.documents), 0)

    def test_news_with_published_at_is_ingested_as_news(self):
        client, mem, _ = _mem_client()
        resp = _ingest(
            client, content=PROSE, source_type="news", source_path="/n.md",
            published_at="2024-01-15",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mem.stats()["by_source_type"]["news"]["documents"], 1)

    def test_adapter_assigned_source_type_rejected_is_400(self):
        client, _, _ = _mem_client()
        resp = _ingest(client, content=PROSE, source_type="chatgpt_conversation", source_path="/x")
        self.assertEqual(resp.status_code, 400)

    def test_oversized_source_path_is_400(self):
        client, _, _ = _mem_client()
        resp = _ingest(client, content=PROSE, source_path="/" + "a" * 2000)
        self.assertEqual(resp.status_code, 400)

    def test_non_object_body_is_400(self):
        client, _, _ = _mem_client()
        resp = client.post("/v1/memory/ingest", headers=_AUTH, json=["not", "an", "object"])
        self.assertEqual(resp.status_code, 400)


class TestAuth(unittest.TestCase):
    def test_missing_bearer_is_401(self):
        client, mem, _ = _mem_client()
        resp = client.post("/v1/memory/ingest", json={"content": PROSE})
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(len(mem.documents), 0)

    def test_wrong_bearer_is_401(self):
        client, _, _ = _mem_client()
        resp = client.post(
            "/v1/memory/ingest", headers={"Authorization": "Bearer nope"},
            json={"content": PROSE},
        )
        self.assertEqual(resp.status_code, 401)


class TestProvenance(unittest.TestCase):
    def test_per_machine_key_sets_source_machine_id(self):
        client, mem, store = _mem_client()
        machine_id, machine_auth = _issue_machine_key(client, store)
        resp = _ingest(client, headers=machine_auth, content=PROSE, source_path="/x.md")
        self.assertEqual(resp.status_code, 200)
        doc, = list(mem.documents.values())
        self.assertEqual(doc["source_machine_id"], machine_id)

    def test_admin_key_uses_body_source_machine_id_or_none(self):
        client, mem, _ = _mem_client()
        # Admin may name the origin machine explicitly.
        _ingest(client, content=PROSE, source_path="/a.md", source_machine_id="mach_named")
        doc_a = mem.documents[next(iter(mem.documents))]
        self.assertEqual(doc_a["source_machine_id"], "mach_named")
        # Admin without a body value -> None provenance.
        client2, mem2, _ = _mem_client()
        _ingest(client2, content=PROSE2, source_path="/b.md")
        doc_b, = list(mem2.documents.values())
        self.assertIsNone(doc_b["source_machine_id"])


class TestAvailabilityGate(unittest.TestCase):
    def test_unavailable_memory_returns_503(self):
        client, mem, _ = _mem_client(memory_embedder=_raise_unavailable)
        resp = _ingest(client, content=PROSE, source_path="/x.md")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error"]["code"], "memory_unavailable")
        # Nothing was written.
        self.assertEqual(len(mem.documents), 0)

    def test_bad_request_still_400_even_when_unavailable(self):
        # Client-error precedence: a malformed body is a 400 before the 503 gate.
        client, _, _ = _mem_client(memory_embedder=_raise_unavailable)
        self.assertEqual(_ingest(client, content="  ").status_code, 400)


class TestChatGPTExport(unittest.TestCase):
    def test_chatgpt_export_content_is_detected_and_ingested(self):
        client, mem, _ = _mem_client()
        fixture = Path(__file__).resolve().parent / "fixtures" / "chatgpt_conversations.json"
        text = fixture.read_text(encoding="utf-8")
        resp = _ingest(client, content=text, source_path="/uploads/export.json").get_json()
        # Two conversations -> two chatgpt_conversation documents (adapter-assigned).
        self.assertEqual(resp["documents_new"], 2)
        self.assertEqual(mem.stats()["by_source_type"]["chatgpt_conversation"]["documents"], 2)
        for doc in mem.documents.values():
            self.assertEqual(doc["source_type"], "chatgpt_conversation")
            self.assertTrue(doc["document_id"].startswith("cg_"))

    def test_in_batch_identical_content_is_deduped_to_one(self):
        # ONE request carrying TWO documents of IDENTICAL content (two ChatGPT
        # conversations with DIFFERENT ids but the same messages) hits the
        # _classify in-batch seen_hashes dedup: only one is written.
        client, mem, _ = _mem_client()
        export = _chatgpt_export(
            _conversation("shared question", "shared answer", conversation_id="conv-A"),
            _conversation("shared question", "shared answer", conversation_id="conv-B"),
        )
        resp = _ingest(client, content=export, source_path="/uploads/dup.json").get_json()
        self.assertEqual(resp["documents_detected"], 2)  # two docs parsed
        self.assertEqual(resp["documents_new"], 1)       # but only one written
        self.assertEqual(resp["documents_unchanged"], 1)  # the other reconciled
        self.assertEqual(len(mem.documents), 1)

    def test_two_anonymous_idless_ingests_do_not_overwrite(self):
        # Regression for the code-review blocker: two SEPARATE anonymous ingests
        # (no source_path) of ID-LESS conversations with DIFFERENT content must
        # produce DISTINCT documents (documents_new each), never a silent overwrite
        # (documents_replaced) via a constant sentinel-derived doc_id.
        client, mem, _ = _mem_client()
        first = _ingest(
            client, content=_chatgpt_export(_conversation("alpha one", "alpha reply")),
        ).get_json()
        second = _ingest(
            client, content=_chatgpt_export(_conversation("beta two", "beta reply")),
        ).get_json()
        self.assertEqual(first["documents_new"], 1)
        self.assertEqual(second["documents_new"], 1)
        self.assertEqual(second["documents_replaced"], 0)
        # Both coexist — no overwrite.
        self.assertEqual(len(mem.documents), 2)
        # Anonymous ingest records no synthetic client path.
        self.assertTrue(all(d["source_path"] is None for d in mem.documents.values()))


if __name__ == "__main__":
    unittest.main()
