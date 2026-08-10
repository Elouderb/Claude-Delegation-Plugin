"""Tests for the Phase-3a cross-encoder reranker.

Two layers, both hermetic and model-free:

  * memory.reranker unit behavior — the module imports without the heavy deps,
    the ImportError path degrades to MemoryUnavailable, and batch-size env
    validation is enforced.  A deterministic stub stands in for the real
    CrossEncoder so nothing downloads a model or loads torch.

  * store.query rerank hook — with the storage extras present (sqlite-vec +
    pysqlite3), prove that rerank=False is byte-identical to the base query, that
    the reranker reorders WITHIN a larger pool (including promoting a candidate
    from beyond top_k), that ties break deterministically by chunk_id, and that
    the contract errors (missing reranker, bad pool, wrong score count) hold.

Nothing here touches the real ~/.agent-os: every store uses an explicit temp
db_path, exactly like test_memory_store.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root: memory_core
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # mcp: memory.store

from memory_core import reranker as reranker_mod  # noqa: E402
from memory_core.availability import (  # noqa: E402
    DEFAULT_RERANKER_MODEL,
    MemoryUnavailable,
    check_availability,
)

_STORAGE = check_availability()["storage_available"]


class StubReranker:
    """Deterministic reranker: score = first matching substring's value, else 0.

    Lets a test force a known post-rerank order without any model.  Ties (equal
    scores) exercise the store's chunk_id tie-break.
    """

    model_name = "stub-reranker-v1"

    def __init__(self, priority=None):
        self._priority = dict(priority or {})

    def score(self, query, texts):
        out = []
        for text in texts:
            value = 0.0
            for sub, val in self._priority.items():
                if sub in text:
                    value = val
                    break
            out.append(value)
        return out


# --------------------------------------------------------------------------- #
# memory.reranker unit tests (no storage extras, no model download).
# --------------------------------------------------------------------------- #
class TestRerankerModule(unittest.TestCase):
    def test_module_imports_without_heavy_deps(self):
        # Importing the module must never pull sentence-transformers/torch.
        self.assertTrue(hasattr(reranker_mod, "CrossEncoderReranker"))
        self.assertTrue(hasattr(reranker_mod, "get_default_reranker"))
        self.assertEqual(DEFAULT_RERANKER_MODEL, "cross-encoder/ms-marco-MiniLM-L-6-v2")

    def test_missing_sentence_transformers_degrades_cleanly(self):
        # With sentence_transformers unimportable, constructing the reranker must
        # raise MemoryUnavailable (which the tool layer turns into a clean
        # "unavailable" result) rather than a bare ImportError.
        with patch.dict(sys.modules, {"sentence_transformers": None}):
            with self.assertRaises(MemoryUnavailable):
                reranker_mod.CrossEncoderReranker()

    def test_bad_batch_env_rejected(self):
        for bad in ("0", "-4", "abc"):
            with patch.dict("os.environ", {reranker_mod._BATCH_ENV: bad}):
                with self.assertRaises(MemoryUnavailable):
                    reranker_mod._resolve_batch_size()

    def test_batch_env_default_and_override(self):
        with patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop(reranker_mod._BATCH_ENV, None)
            self.assertEqual(reranker_mod._resolve_batch_size(), reranker_mod._DEFAULT_BATCH)
        with patch.dict("os.environ", {reranker_mod._BATCH_ENV: "8"}):
            self.assertEqual(reranker_mod._resolve_batch_size(), 8)


# A small recorded-vector corpus so rerank behavior is proven end-to-end without
# a real embedder (mirrors the StubEmbedder in test_memory_store).
DIM = 8


def _e(i: int) -> list:
    v = [0.0] * DIM
    v[i] = 1.0
    return v


CATS = "A small domesticated feline that purrs when it is content."
DOGS = "A loyal canine companion that barks at approaching strangers."
FIN = "Quarterly ledger reconciliation and revenue projection notes."
WIDGET = "Replacement part serial ZX-9000 currently held in warehouse stock."
Q_PARA = "house cat kitten pet animal"  # paraphrase of CATS, zero lexical overlap


class _RecordedEmbedder:
    """Deterministic embedder: exact text -> recorded unit vector, else default."""

    model_name = "stub-recorded-v1"

    def __init__(self, vectors: dict, dim: int = DIM):
        self._vectors = {k.strip(): v for k, v in vectors.items()}
        self._dim = dim
        self._default = _e(dim - 1)

    @property
    def dim(self) -> int:
        return self._dim

    def _lookup(self, text: str):
        return self._vectors.get((text or "").strip(), self._default)

    def embed_documents(self, texts):
        return [self._lookup(t) for t in texts]

    def embed_query(self, text):
        return self._lookup(text)


def _corpus_embedder():
    return _RecordedEmbedder(
        {CATS: _e(0), DOGS: _e(1), FIN: _e(2), WIDGET: _e(3), Q_PARA: _e(0)}
    )


@unittest.skipUnless(_STORAGE, "memory storage extras (sqlite-vec + pysqlite3) not installed")
class TestStoreRerankHook(unittest.TestCase):
    def setUp(self):
        from memory.store import MemoryStore

        self._tmp = tempfile.TemporaryDirectory(prefix="agent_os_rerank_")
        self.root = Path(self._tmp.name)
        corpus = self.root / "corpus"
        corpus.mkdir()
        (corpus / "cats.txt").write_text(CATS, encoding="utf-8")
        (corpus / "dogs.txt").write_text(DOGS, encoding="utf-8")
        (corpus / "finance.txt").write_text(FIN, encoding="utf-8")
        (corpus / "widget.txt").write_text(WIDGET, encoding="utf-8")
        self.store = MemoryStore(self.root / "memory.sqlite", embedding_dim=DIM)
        self.embedder = _corpus_embedder()
        self.store.ingest_path(str(corpus), self.embedder)

    def tearDown(self):
        self._tmp.cleanup()

    def _titles(self, results):
        return [r["title"] for r in results]

    def test_rerank_false_is_byte_identical_to_base(self):
        base = self.store.query(self.embedder, Q_PARA, top_k=4)
        explicit_off = self.store.query(self.embedder, Q_PARA, top_k=4, rerank=False)
        self.assertEqual(base, explicit_off)
        # Base results never carry a rerank_score key.
        self.assertTrue(all("rerank_score" not in r for r in base))

    def test_rerank_reorders_pool_and_annotates_score(self):
        # Hybrid ranks CATS first for Q_PARA; a reranker that prefers the widget
        # (its text contains "warehouse") must surface WIDGET on top instead.
        base = self.store.query(self.embedder, Q_PARA, top_k=4)
        self.assertEqual(self._titles(base)[0], "cats")

        reranked = self.store.query(
            self.embedder, Q_PARA, top_k=4, rerank=True,
            reranker=StubReranker({"warehouse": 10.0}),
        )
        self.assertEqual(self._titles(reranked)[0], "widget")
        # Every reranked row is annotated, ordered by rerank_score descending.
        self.assertTrue(all("rerank_score" in r for r in reranked))
        scores = [r["rerank_score"] for r in reranked]
        self.assertEqual(scores, sorted(scores, reverse=True))
        # The RRF fusion score is preserved alongside the rerank score.
        self.assertTrue(all("score" in r for r in reranked))

    def test_rerank_promotes_candidate_from_beyond_top_k(self):
        # top_k=1 base returns CATS (hybrid #1). A pool that spans all 4 docs plus
        # a reranker preferring the widget promotes WIDGET (hybrid #4) to #1 —
        # only possible because rerank fuses a pool larger than top_k.
        base = self.store.query(self.embedder, Q_PARA, top_k=1)
        self.assertEqual(self._titles(base), ["cats"])

        reranked = self.store.query(
            self.embedder, Q_PARA, top_k=1, rerank=True, rerank_pool=10,
            reranker=StubReranker({"warehouse": 10.0}),
        )
        self.assertEqual(self._titles(reranked), ["widget"])

    def test_rerank_tie_break_is_deterministic_by_chunk_id(self):
        # All-equal rerank scores -> order must fall back to chunk_id ascending.
        reranked = self.store.query(
            self.embedder, Q_PARA, top_k=4, rerank=True, reranker=StubReranker({}),
        )
        chunk_ids = [r["chunk_id"] for r in reranked]
        self.assertEqual(chunk_ids, sorted(chunk_ids))

    def test_rerank_true_without_reranker_raises(self):
        with self.assertRaises(ValueError):
            self.store.query(self.embedder, Q_PARA, top_k=4, rerank=True)

    def test_bad_rerank_pool_rejected(self):
        bad_pools: list = [0, -1, True, "50"]  # intentionally invalid types/values
        for bad in bad_pools:
            with self.assertRaises(ValueError):
                self.store.query(
                    self.embedder, Q_PARA, top_k=2, rerank=True,
                    reranker=StubReranker(), rerank_pool=bad,
                )

    def test_reranker_score_count_mismatch_raises(self):
        class BadReranker:
            model_name = "bad"

            def score(self, query, texts):
                return [1.0]  # wrong length on purpose

        with self.assertRaises(ValueError):
            self.store.query(
                self.embedder, Q_PARA, top_k=4, rerank=True, reranker=BadReranker(),
            )

    def test_empty_store_rerank_returns_empty(self):
        from memory.store import MemoryStore

        empty = MemoryStore(self.root / "empty.sqlite", embedding_dim=DIM)
        self.assertEqual(
            empty.query(self.embedder, "anything", rerank=True, reranker=StubReranker()),
            [],
        )


class _AllSameEmbedder:
    """Every text embeds to the SAME unit vector, so fusion admits every chunk to
    the pool (equidistant) and the RERANKER alone decides order."""

    model_name = "all-same-v1"

    def __init__(self, dim=DIM):
        self._dim = dim
        self._v = _e(0)

    @property
    def dim(self):
        return self._dim

    def embed_documents(self, texts):
        return [list(self._v) for _ in texts]

    def embed_query(self, text):
        return list(self._v)


@unittest.skipUnless(_STORAGE, "memory storage extras (sqlite-vec + pysqlite3) not installed")
class TestRerankDedupComposition(unittest.TestCase):
    """Dedup is applied AFTER rerank, on the reranked order: the highest-RERANKED
    chunk of each doc is the one kept, and it de-crowds a reranked top_k."""

    # docA has three chunks, docB one.  Each carries a unique marker the stub
    # reranker scores on, so the reranked order is fully determined.
    A0 = "alpha zero AZERO"
    A1 = "alpha one AONE"
    A2 = "alpha two KEEPER"
    B0 = "bravo zero BRAVO"

    def setUp(self):
        from memory.store import MemoryStore
        from memory_core.adapters.base import ChunkInput, SourceDocument, sha256_text

        self._tmp = tempfile.TemporaryDirectory(prefix="agent_os_rrdd_")
        self.root = Path(self._tmp.name)
        self.store = MemoryStore(self.root / "memory.sqlite", embedding_dim=DIM)
        self.embedder = _AllSameEmbedder()

        def _doc(doc_id, texts):
            return SourceDocument(
                doc_id=doc_id, title=doc_id, source_type="document",
                source_path=f"/virtual/{doc_id}.txt",
                content_hash=sha256_text("|".join(texts)),
                provenance="uploaded", published_at=None,
                chunks=[ChunkInput(seq=i, text=t) for i, t in enumerate(texts)],
            )

        self.store.ingest_documents(
            [_doc("docA", [self.A0, self.A1, self.A2]), _doc("docB", [self.B0])],
            self.embedder,
        )
        # Reranked order (score desc, chunk_id tie-break): A2(5) > A0(4) > B0(3) > A1(1).
        self.reranker = StubReranker({"KEEPER": 5.0, "AZERO": 4.0, "BRAVO": 3.0, "AONE": 1.0})

    def tearDown(self):
        self._tmp.cleanup()

    def _docs(self, results):
        return [r["doc_id"] for r in results]

    def test_rerank_only_top_k_is_doc_crowded(self):
        # Rerank alone puts TWO docA chunks (A2, A0) in the top-2 -> crowded.
        res = self.store.query(
            self.embedder, "q", top_k=2, rerank=True, reranker=self.reranker, rerank_pool=10)
        self.assertEqual(self._docs(res), ["docA", "docA"])
        self.assertIn("KEEPER", res[0]["text"])
        self.assertIn("AZERO", res[1]["text"])

    def test_rerank_plus_dedup_keeps_top_reranked_chunk_per_doc(self):
        # Same rerank order, but dedup(max_per_doc=1) drops the 2nd docA chunk and
        # backfills docB -> the reranked top_k becomes distinct docs, and the docA
        # slot is the TOP-reranked docA chunk (A2/KEEPER), not A0.
        res = self.store.query(
            self.embedder, "q", top_k=2, rerank=True, reranker=self.reranker,
            rerank_pool=10, max_per_doc=1)
        self.assertEqual(self._docs(res), ["docA", "docB"])
        self.assertIn("KEEPER", res[0]["text"])
        self.assertIn("BRAVO", res[1]["text"])
        # Reranked annotations survive dedup; order is by rerank_score descending.
        self.assertTrue(all("rerank_score" in r for r in res))
        scores = [r["rerank_score"] for r in res]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_rerank_dedup_never_exceeds_cap(self):
        res = self.store.query(
            self.embedder, "q", top_k=4, rerank=True, reranker=self.reranker,
            rerank_pool=10, max_per_doc=1)
        docs = self._docs(res)
        self.assertEqual(len(docs), len(set(docs)))  # all distinct
        self.assertEqual(set(docs), {"docA", "docB"})


if __name__ == "__main__":
    unittest.main()
