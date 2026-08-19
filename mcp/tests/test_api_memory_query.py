"""Tests for the central memory READ path (card b81ef155, epic 6fead93b, Slice 2).

Hermetic: a Flask test client over a FAKE in-memory memory store
(:class:`FakeMemoryStore`) + deterministic fake embedder/reranker — NO SQL Server,
NO real model, NO network, and NO real corpus (every fixture is synthetic). Two
layers are covered:

  * :meth:`FakeMemoryStore.query` — RRF fusion order, each filter (source_type +
    date range incl. the created_at fallback), per-doc dedup (``max_per_doc``),
    rerank on/off, argument validation, and row-shape parity with the LOCAL store.
  * ``POST /v1/memory/query`` + ``GET /v1/memory/status`` via injected fakes —
    body validation (400), auth (401), the embedder/reranker availability gate
    (503), and the happy-path 200 result shape.

A GATED live test (:class:`MemoryQueryLiveTest`, skipped unless
``AGENT_OS_RUN_LIVE_DB_TESTS=1`` AND the central-DB connection string is set) runs
a REAL VECTOR_DISTANCE + CONTAINSTABLE query against the live FT-indexed corpus.
It is READ-ONLY (never writes) and is run by the lead, not this suite.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_core.availability import (  # noqa: E402
    DEFAULT_EMBEDDING_DIM,
    MemoryUnavailable,
    check_availability,
)
from services.api.app import create_app  # noqa: E402
from services.api.memory_store import FakeMemoryStore, _bm25_rerank  # noqa: E402
from services.api.store import FakeStore  # noqa: E402

_KEY = "test-api-key"
_AUTH = {"Authorization": f"Bearer {_KEY}"}
_DIM = 4


# --------------------------------------------------------------------------- #
# Deterministic fakes (no torch, no SQL)
# --------------------------------------------------------------------------- #
class QueryVecEmbedder:
    """Returns a FIXED query vector, so the seeded chunk vectors alone decide the
    cosine ranking. ``dim``/``model_name`` satisfy the store's dim validation."""

    def __init__(self, query_vec, model_name="fake-embedder-v1"):
        self._vec = [float(x) for x in query_vec]
        self.model_name = model_name

    @property
    def dim(self) -> int:
        return len(self._vec)

    def embed_query(self, text):
        return list(self._vec)

    def embed_documents(self, texts):
        return [list(self._vec) for _ in texts]


class FakeReranker:
    """Cross-encoder stub: score = 1.0 when ``_PROMOTE`` appears in the chunk text,
    else 0.0 — so a test can force a specific chunk above the fusion winner."""

    model_name = "fake-reranker-v1"

    def score(self, query, texts):
        return [1.0 if "PROMOTE" in (t or "") else 0.0 for t in texts]


def _raise_unavailable():
    raise MemoryUnavailable("memory subsystem unavailable (test)")


def _seed(mem, doc_id, chunks, *, source_type="document", published_at=None,
          source_path=None, title="T", created_at=None, provenance="uploaded"):
    """Seed one synthetic document + chunks directly into the fake store.

    ``chunks`` is a list of ``(seq, text, vector)`` (or ``(seq, text, vector, meta)``).
    Deliberately bypasses ingest so vectors/dates are fully controlled.
    """
    mem.documents[doc_id] = {
        "document_id": doc_id, "title": title, "source_type": source_type,
        "source_path": source_path, "content_hash": f"hash-{doc_id}",
        "source_machine_id": None, "provenance": provenance,
        "published_at": published_at, "created_at": created_at,
    }
    for spec in chunks:
        seq, text, vec = spec[0], spec[1], spec[2]
        meta = spec[3] if len(spec) > 3 else {}
        cid = f"mem:{doc_id}:{seq}"
        mem.chunks[cid] = {
            "chunk_id": cid, "document_id": doc_id, "seq": seq,
            "chunk_text": text, "meta": meta,
            "embedding_model": "fake-embedder-v1", "embedding_dim": mem.embedding_dim,
        }
        mem.vectors[cid] = [float(x) for x in vec]


def _ids(results):
    return [r["chunk_id"] for r in results]


# The canonical LOCAL row shape (mcp/memory/store.py::MemoryStore._result_row
# L803-823). Duplicated here rather than imported to keep the test hermetic and
# clear of the ``mcp`` package-name collision with the external mcp library; if the
# local shape changes, this list must change with it.
_LOCAL_ROW_KEYS = {
    "chunk_id", "text", "seq", "score", "doc_id", "title", "source_type",
    "published_at", "ingested_at", "source_path", "provenance",
    "embedding_model", "meta",
}


# --------------------------------------------------------------------------- #
# _bm25_rerank — the hub-only pool-local BM25 lexical re-rank (pure, no DB)
# --------------------------------------------------------------------------- #
class TestBm25Rerank(unittest.TestCase):
    """The pure helper that fixes the CONTAINSTABLE-RANK-vs-FTS5-bm25() regression
    (card b81ef155). Deterministic; no DB, no corpus — synthetic docs only."""

    def test_more_matches_rank_above_fewer(self):
        # c2 matches "apple" three times, c1 matches "banana" once, c3 matches none.
        docs = [
            ("c1", "banana bread"),
            ("c2", "apple apple apple pie"),
            ("c3", "cherry tart"),
        ]
        order = _bm25_rerank(["apple", "banana"], docs)
        self.assertEqual(order, ["c2", "c1", "c3"])

    def test_rarer_term_outranks_common_term(self):
        # "rare" occurs in one doc (high IDF); "common" in three (low IDF). With
        # equal tf and length, the rare-term doc must sort first; the common-term
        # docs tie and keep their input (RANK) order.
        docs = [
            ("c1", "common word"),
            ("c2", "rare word"),
            ("c3", "common thing"),
            ("c4", "common stuff"),
        ]
        order = _bm25_rerank(["common", "rare"], docs)
        self.assertEqual(order, ["c2", "c1", "c3", "c4"])

    def test_higher_tf_outranks_lower_tf_same_term(self):
        docs = [
            ("lo", "alpha beta"),
            ("hi", "alpha alpha alpha beta"),
        ]
        order = _bm25_rerank(["alpha"], docs)
        self.assertEqual(order[0], "hi")

    def test_empty_terms_preserves_input_order(self):
        docs = [("c1", "x y"), ("c2", "z w")]
        self.assertEqual(_bm25_rerank([], docs), ["c1", "c2"])

    def test_blank_only_terms_preserve_input_order(self):
        docs = [("c1", "x y"), ("c2", "z w")]
        self.assertEqual(_bm25_rerank(["", "  "], docs), ["c1", "c2"])

    def test_empty_docs_returns_empty(self):
        self.assertEqual(_bm25_rerank(["anything"], []), [])

    def test_no_term_matches_preserves_input_order(self):
        docs = [("c1", "alpha beta"), ("c2", "gamma delta")]
        self.assertEqual(_bm25_rerank(["zzz", "qqq"], docs), ["c1", "c2"])

    def test_case_insensitive(self):
        docs = [("c1", "APPLE Apple apple"), ("c2", "pear")]
        self.assertEqual(_bm25_rerank(["ApPlE"], docs), ["c1", "c2"])

    def test_none_text_does_not_crash(self):
        docs = [("c1", None), ("c2", "apple apple")]
        self.assertEqual(_bm25_rerank(["apple"], docs), ["c2", "c1"])

    def test_deterministic_repeated_calls(self):
        docs = [
            ("c1", "banana bread"),
            ("c2", "apple apple apple pie"),
            ("c3", "cherry tart"),
        ]
        first = _bm25_rerank(["apple", "banana"], docs)
        second = _bm25_rerank(["apple", "banana"], docs)
        self.assertEqual(first, second)


# --------------------------------------------------------------------------- #
# FakeMemoryStore.query — the read path, no HTTP
# --------------------------------------------------------------------------- #
class TestFakeStoreQuery(unittest.TestCase):
    def _three_doc_store(self):
        mem = FakeMemoryStore(embedding_dim=_DIM)
        _seed(mem, "A", [(0, "apple pie recipe", [1, 0, 0, 0])])
        _seed(mem, "B", [(0, "banana split dessert", [0, 1, 0, 0])])
        _seed(mem, "C", [(0, "cherry tart", [-1, 0, 0, 0])])
        return mem

    def test_fusion_order_and_rrf_score(self):
        mem = self._three_doc_store()
        emb = QueryVecEmbedder([1, 0, 0, 0])
        results = mem.query("apple banana", emb, top_k=8, max_per_doc=None)
        # Vector ranks A(d0)>B(d1)>C(d2); lexical matches A("apple")>B("banana");
        # RRF: A=2/61 > B=2/62 > C=1/63.
        self.assertEqual(_ids(results), ["mem:A:0", "mem:B:0", "mem:C:0"])
        self.assertEqual(results[0]["score"], round(2 / 61, 6))
        self.assertEqual(results[0]["doc_id"], "A")

    def test_punctuation_query_uses_vector_only(self):
        # No word characters -> no lexical terms -> lexical arm skipped (mirrors the
        # local fts_ids=[] path); vector kNN still returns a ranked candidate set.
        mem = self._three_doc_store()
        emb = QueryVecEmbedder([1, 0, 0, 0])
        results = mem.query("!!! ???", emb, top_k=8, max_per_doc=None)
        self.assertEqual(_ids(results), ["mem:A:0", "mem:B:0", "mem:C:0"])

    def test_source_type_filter(self):
        mem = FakeMemoryStore(embedding_dim=_DIM)
        _seed(mem, "DOC", [(0, "shared topic", [1, 0, 0, 0])], source_type="document")
        _seed(mem, "NEWS", [(0, "shared topic", [1, 0, 0, 0])], source_type="news",
              published_at="2024-01-01")
        emb = QueryVecEmbedder([1, 0, 0, 0])
        results = mem.query("shared topic", emb, top_k=8, source_type="news", max_per_doc=None)
        self.assertTrue(results)
        self.assertTrue(all(r["source_type"] == "news" for r in results))
        self.assertEqual({r["doc_id"] for r in results}, {"NEWS"})

    def test_date_range_filter_on_published_at(self):
        mem = FakeMemoryStore(embedding_dim=_DIM)
        _seed(mem, "OLD", [(0, "topic", [1, 0, 0, 0])], published_at="2024-01-01")
        _seed(mem, "NEW", [(0, "topic", [1, 0, 0, 0])], published_at="2024-12-31")
        emb = QueryVecEmbedder([1, 0, 0, 0])
        results = mem.query("topic", emb, top_k=8, date_from="2024-06-01", max_per_doc=None)
        self.assertEqual({r["doc_id"] for r in results}, {"NEW"})

    def test_date_fallback_to_created_at(self):
        # published_at is NULL -> the filter falls back to created_at (the central
        # equivalent of the local ingested_at), matched on its YYYY-MM-DD prefix.
        mem = FakeMemoryStore(embedding_dim=_DIM)
        created = datetime(2024, 3, 15, 12, 0, tzinfo=timezone.utc)
        _seed(mem, "D", [(0, "topic", [1, 0, 0, 0])], published_at=None, created_at=created)
        emb = QueryVecEmbedder([1, 0, 0, 0])
        inside = mem.query("topic", emb, date_from="2024-03-01", date_to="2024-03-31",
                           max_per_doc=None)
        self.assertEqual({r["doc_id"] for r in inside}, {"D"})
        outside = mem.query("topic", emb, date_from="2024-04-01", max_per_doc=None)
        self.assertEqual(outside, [])

    def _dedup_store(self):
        mem = FakeMemoryStore(embedding_dim=_DIM)
        _seed(mem, "A", [
            (0, "apple one", [1, 0, 0, 0]),
            (1, "apple two", [1, 0, 0, 0]),
            (2, "apple three", [1, 0, 0, 0]),
        ])
        _seed(mem, "B", [(0, "apple four", [0, 1, 0, 0])])
        return mem

    def test_dedup_max_per_doc_1_keeps_distinct_docs(self):
        mem = self._dedup_store()
        emb = QueryVecEmbedder([1, 0, 0, 0])
        results = mem.query("apple", emb, top_k=8, max_per_doc=1)
        self.assertEqual(_ids(results), ["mem:A:0", "mem:B:0"])
        self.assertEqual([r["doc_id"] for r in results], ["A", "B"])

    def test_no_dedup_returns_multiple_chunks_per_doc(self):
        mem = self._dedup_store()
        emb = QueryVecEmbedder([1, 0, 0, 0])
        results = mem.query("apple", emb, top_k=8, max_per_doc=None)
        self.assertEqual(_ids(results), ["mem:A:0", "mem:A:1", "mem:A:2", "mem:B:0"])

    def test_rerank_reorders_and_annotates(self):
        mem = FakeMemoryStore(embedding_dim=_DIM)
        _seed(mem, "A", [(0, "apple winner", [1, 0, 0, 0])])
        _seed(mem, "B", [(0, "apple PROMOTE me", [0, 1, 0, 0])])
        emb = QueryVecEmbedder([1, 0, 0, 0])
        base = mem.query("apple", emb, top_k=8, max_per_doc=None)
        self.assertEqual(_ids(base), ["mem:A:0", "mem:B:0"])  # A wins on fusion
        reranked = mem.query("apple", emb, top_k=8, rerank=True, reranker=FakeReranker(),
                             max_per_doc=None)
        # The reranker promotes B above A and annotates rerank_score on every row,
        # leaving the RRF score intact.
        self.assertEqual(_ids(reranked), ["mem:B:0", "mem:A:0"])
        self.assertEqual(reranked[0]["rerank_score"], 1.0)
        self.assertEqual(reranked[1]["rerank_score"], 0.0)
        self.assertIn("score", reranked[0])

    def test_row_shape_parity_with_local(self):
        mem = FakeMemoryStore(embedding_dim=_DIM)
        _seed(mem, "A", [(0, "apple", [1, 0, 0, 0], {"role": "assistant"})],
              published_at="2024-01-01", source_path="/x.md")
        emb = QueryVecEmbedder([1, 0, 0, 0])
        (row,) = mem.query("apple", emb, top_k=8, max_per_doc=None)
        self.assertEqual(set(row.keys()), _LOCAL_ROW_KEYS)
        self.assertEqual(row["text"], "apple")
        self.assertEqual(row["doc_id"], "A")
        self.assertEqual(row["meta"], {"role": "assistant"})
        # rerank adds exactly one key.
        (rr,) = mem.query("apple", emb, top_k=8, rerank=True, reranker=FakeReranker(),
                          max_per_doc=None)
        self.assertEqual(set(rr.keys()), _LOCAL_ROW_KEYS | {"rerank_score"})

    def test_empty_corpus_returns_empty(self):
        mem = FakeMemoryStore(embedding_dim=_DIM)
        emb = QueryVecEmbedder([1, 0, 0, 0])
        self.assertEqual(mem.query("anything", emb), [])

    def test_validation_errors(self):
        mem = self._three_doc_store()
        emb = QueryVecEmbedder([1, 0, 0, 0])
        with self.assertRaises(ValueError):
            mem.query("q", emb, top_k=0)
        with self.assertRaises(ValueError):
            mem.query("q", emb, source_type="not-a-type")
        with self.assertRaises(ValueError):
            mem.query("q", emb, date_from="nonsense")
        with self.assertRaises(ValueError):
            mem.query("q", emb, max_per_doc=0)
        with self.assertRaises(ValueError):
            mem.query("q", emb, rerank=True, reranker=None)
        with self.assertRaises(ValueError):
            mem.query("q", QueryVecEmbedder([1, 0]))  # dim 2 != store dim 4


# --------------------------------------------------------------------------- #
# Endpoint tests (POST /v1/memory/query, GET /v1/memory/status)
# --------------------------------------------------------------------------- #
def _client(*, memory_store=None, memory_embedder=None, memory_reranker=None,
            api_key=_KEY, store=None):
    store = store if store is not None else FakeStore()
    memory_store = memory_store if memory_store is not None else FakeMemoryStore(embedding_dim=_DIM)
    if memory_embedder is None:
        memory_embedder = lambda: QueryVecEmbedder([1, 0, 0, 0])  # noqa: E731
    if memory_reranker is None:
        memory_reranker = lambda: FakeReranker()  # noqa: E731
    app = create_app(
        api_key=api_key, store=store, memory_store=memory_store,
        memory_embedder=memory_embedder, memory_reranker=memory_reranker,
    )
    app.testing = True
    return app.test_client(), memory_store


def _seeded_store():
    mem = FakeMemoryStore(embedding_dim=_DIM)
    _seed(mem, "A", [
        (0, "apple one", [1, 0, 0, 0]),
        (1, "apple two", [1, 0, 0, 0]),
    ])
    _seed(mem, "B", [(0, "apple three", [0, 1, 0, 0])])
    return mem


class TestQueryEndpoint(unittest.TestCase):
    def test_happy_path_200_shape(self):
        client, _ = _client(memory_store=_seeded_store())
        resp = client.post("/v1/memory/query", headers=_AUTH, json={"query": "apple"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["available"])
        self.assertEqual(body["operation"], "memory_query")
        self.assertEqual(body["query"], "apple")
        self.assertEqual(body["top_k"], 8)
        self.assertFalse(body["reranked"])
        self.assertEqual(body["max_per_doc"], 1)  # default dedup ON
        self.assertEqual(body["count"], len(body["results"]))
        self.assertTrue(body["results"])
        self.assertEqual(set(body["results"][0].keys()), _LOCAL_ROW_KEYS)

    def test_default_max_per_doc_dedups_to_one_per_doc(self):
        client, _ = _client(memory_store=_seeded_store())
        body = client.post("/v1/memory/query", headers=_AUTH, json={"query": "apple"}).get_json()
        # Default max_per_doc=1 -> distinct docs A, B (not two chunks of A).
        self.assertEqual([r["doc_id"] for r in body["results"]], ["A", "B"])

    def test_explicit_null_max_per_doc_disables_dedup(self):
        client, _ = _client(memory_store=_seeded_store())
        body = client.post(
            "/v1/memory/query", headers=_AUTH, json={"query": "apple", "max_per_doc": None},
        ).get_json()
        self.assertIsNone(body["max_per_doc"])
        self.assertEqual(_ids(body["results"]), ["mem:A:0", "mem:A:1", "mem:B:0"])

    def test_rerank_true_uses_injected_reranker(self):
        mem = FakeMemoryStore(embedding_dim=_DIM)
        _seed(mem, "A", [(0, "apple winner", [1, 0, 0, 0])])
        _seed(mem, "B", [(0, "apple PROMOTE", [0, 1, 0, 0])])
        client, _ = _client(memory_store=mem)
        body = client.post(
            "/v1/memory/query", headers=_AUTH,
            json={"query": "apple", "rerank": True, "max_per_doc": None},
        ).get_json()
        self.assertTrue(body["reranked"])
        self.assertEqual(_ids(body["results"]), ["mem:B:0", "mem:A:0"])
        self.assertIn("rerank_score", body["results"][0])

    def test_requires_auth(self):
        client, _ = _client(memory_store=_seeded_store())
        self.assertEqual(client.post("/v1/memory/query", json={"query": "apple"}).status_code, 401)

    def test_validation_400s(self):
        client, _ = _client()
        bad_bodies = [
            ["not", "an", "object"],                # non-object body
            {},                                     # missing query
            {"query": "   "},                       # blank query
            {"query": 5},                           # non-str query
            {"query": "q", "top_k": 0},             # top_k < 1
            {"query": "q", "top_k": 10_000},        # top_k > cap
            {"query": "q", "top_k": True},          # bool is not an int here
            {"query": "q", "source_type": 5},       # non-str source_type
            {"query": "q", "rerank": "yes"},        # non-bool rerank
            {"query": "q", "rerank_pool": 0},       # rerank_pool < 1
            {"query": "q", "max_per_doc": 0},       # max_per_doc < 1
            {"query": "q", "date_from": 5},         # non-str date
            {"query": "x" * 4097},                  # query over the length cap
        ]
        for body in bad_bodies:
            resp = client.post("/v1/memory/query", headers=_AUTH, json=body)
            self.assertEqual(resp.status_code, 400, body)

    def test_bad_iso_date_is_400_from_store(self):
        # A well-typed but non-ISO date passes isinstance but the store rejects it;
        # the endpoint maps that ValueError to a 400 (the message echoes only the
        # caller's own input, never corpus text).
        client, _ = _client(memory_store=_seeded_store())
        resp = client.post(
            "/v1/memory/query", headers=_AUTH, json={"query": "apple", "date_from": "13/2024"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_embedder_unavailable_503(self):
        client, _ = _client(memory_store=_seeded_store(), memory_embedder=_raise_unavailable)
        resp = client.post("/v1/memory/query", headers=_AUTH, json={"query": "apple"})
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error"]["code"], "memory_unavailable")

    def test_reranker_unavailable_503(self):
        client, _ = _client(memory_store=_seeded_store(), memory_reranker=_raise_unavailable)
        resp = client.post(
            "/v1/memory/query", headers=_AUTH, json={"query": "apple", "rerank": True},
        )
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.get_json()["error"]["code"], "memory_unavailable")

    def test_bad_request_precedes_unavailable(self):
        # Client-error precedence: a malformed body is a 400 before the 503 gate.
        client, _ = _client(memory_embedder=_raise_unavailable)
        self.assertEqual(
            client.post("/v1/memory/query", headers=_AUTH, json={"query": "  "}).status_code, 400
        )


class TestStatusEndpoint(unittest.TestCase):
    def test_status_200_shape(self):
        mem = _seeded_store()
        client, _ = _client(memory_store=mem)
        resp = client.get("/v1/memory/status", headers=_AUTH)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["operation"], "memory_status")
        self.assertEqual(body["embedding_dim"], DEFAULT_EMBEDDING_DIM)
        self.assertIn("embedding_model", body)
        self.assertIn("reranker_model", body)
        # Corpus stats come straight from the store's stats().
        self.assertEqual(body["corpus"], mem.stats())
        self.assertEqual(body["corpus"]["total_documents"], 2)

    def test_status_requires_auth(self):
        client, _ = _client()
        self.assertEqual(client.get("/v1/memory/status").status_code, 401)

    def test_status_never_500s_on_stats_failure(self):
        class _BoomStore:
            def stats(self):
                raise RuntimeError("db down (should not leak)")
        client, _ = _client(memory_store=_BoomStore())
        resp = client.get("/v1/memory/status", headers=_AUTH)
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("corpus_error", body)
        self.assertNotIn("db down", body["corpus_error"])  # no internal leak


# --------------------------------------------------------------------------- #
# GATED live test — REAL VECTOR_DISTANCE + CONTAINSTABLE, READ-ONLY.
# Skipped unless AGENT_OS_RUN_LIVE_DB_TESTS=1 AND the connection string is set AND
# the embedder is installed. The lead runs this after migration 009 is applied.
# --------------------------------------------------------------------------- #
try:
    from database import migrate as _migrate  # noqa: E402
    _ENV_VAR = _migrate.ENV_VAR
except Exception:  # pragma: no cover - database module import optional
    _ENV_VAR = "AGENT_OS_CENTRAL_DB_CONNECTION_STRING"

_HAS_CENTRAL_DB = bool(os.environ.get(_ENV_VAR)) and (
    os.environ.get("AGENT_OS_RUN_LIVE_DB_TESTS") == "1"
)
_EMBEDDER_AVAILABLE = bool(check_availability().get("embedder_available"))


@unittest.skipUnless(
    _HAS_CENTRAL_DB and _EMBEDDER_AVAILABLE,
    f"live memory query test requires {_ENV_VAR} + AGENT_OS_RUN_LIVE_DB_TESTS=1 + the embedder",
)
class MemoryQueryLiveTest(unittest.TestCase):
    """Exercise the REAL hybrid read path against the live FT-indexed corpus.

    Read-only: query() issues only SELECTs on a per-op connection that is closed
    (never committed), so this test writes nothing. The query string is a generic
    English phrase (never corpus-identifying) — vector kNN guarantees a candidate
    set regardless, so a successful, ranked, non-empty result also proves the
    VECTOR_DISTANCE double-cast marshaling and the CONTAINSTABLE full-text arm both
    execute against the live schema.
    """

    def test_live_hybrid_query_returns_ranked_results(self):
        from database import migrate as db_migrate
        from memory_core.embeddings import get_default_embedder
        from services.api.memory_store import MemoryStore

        store = MemoryStore(db_migrate.connect, embedding_dim=DEFAULT_EMBEDDING_DIM)
        embedder = get_default_embedder()
        results = store.query("software system design", embedder, top_k=5, max_per_doc=None)
        self.assertTrue(results, "live hybrid query returned no results")
        self.assertLessEqual(len(results), 5)
        # Rows carry the full shape and fusion scores are non-increasing.
        for row in results:
            self.assertEqual(set(row.keys()), _LOCAL_ROW_KEYS)
        scores = [r["score"] for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))


if __name__ == "__main__":
    unittest.main()
