"""Integration tests for memory.store: ingest -> hybrid query round trip.

These require the storage stack (sqlite-vec + a loadable-extension-capable
SQLite); when the optional extras are absent they skip, exactly like the db_*
tests skip without pyodbc.  The embedder is a deterministic recorded-vector
STUB (no model download / torch), which is the card-sanctioned fallback and lets
us prove BM25-only and vector-only retrieval deterministically.

Nothing here touches the real ~/.agent-os: every store is created with an
explicit temp db_path.
"""
import math
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root: memory_core
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # mcp: memory.store

from memory_core.availability import check_availability  # noqa: E402

_STORAGE = check_availability()["storage_available"]

DIM = 8


def _e(i: int) -> list:
    """Unit basis vector e_i in R^DIM."""
    v = [0.0] * DIM
    v[i] = 1.0
    return v


def _normalize(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else list(v)


# Corpus chunk texts (each file is a single short sentence -> one chunk whose
# text equals the sentence, so the stub can key vectors on exact text).
CATS = "A small domesticated feline that purrs when it is content."
DOGS = "A loyal canine companion that barks at approaching strangers."
FIN = "Quarterly ledger reconciliation and revenue projection notes."
WIDGET = "Replacement part serial ZX-9000 currently held in warehouse stock."

Q_PARA = "house cat kitten pet animal"   # paraphrase of CATS, zero lexical overlap
Q_ID = "ZX-9000"                          # exact identifier, lexically in WIDGET only


class StubEmbedder:
    """Deterministic embedder: exact-text -> recorded vector, else default.

    Crucially Q_PARA and Q_ID map to the SAME query vector (feline axis), so the
    ONLY thing distinguishing their retrieval is the BM25 signal.
    """

    def __init__(self, vectors: dict, dim: int = DIM, default=None):
        self._vectors = {k.strip(): _normalize(v) for k, v in vectors.items()}
        self._dim = dim
        self._default = _normalize(default or _e(dim - 1))
        self.model_name = "stub-recorded-v1"

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
    return StubEmbedder(
        {
            CATS: _e(0),
            DOGS: _e(1),
            FIN: _e(2),
            WIDGET: _e(3),
            Q_PARA: _e(0),  # -> nearest to CATS
            Q_ID: _e(0),    # -> also nearest to CATS (NOT widget), by design
        }
    )


@unittest.skipUnless(
    _STORAGE,
    "memory storage extras (sqlite-vec + pysqlite3) not installed",
)
class MemoryStoreTestBase(unittest.TestCase):
    def setUp(self):
        from memory.store import MemoryStore

        self._tmp = tempfile.TemporaryDirectory(prefix="agent_os_memstore_")
        self.root = Path(self._tmp.name)
        self.corpus = self.root / "corpus"
        self.corpus.mkdir()
        (self.corpus / "cats.txt").write_text(CATS, encoding="utf-8")
        (self.corpus / "dogs.txt").write_text(DOGS, encoding="utf-8")
        (self.corpus / "finance.txt").write_text(FIN, encoding="utf-8")
        (self.corpus / "widget.txt").write_text(WIDGET, encoding="utf-8")
        self.db_path = self.root / "memory.sqlite"
        self.store = MemoryStore(self.db_path, embedding_dim=DIM)
        self.embedder = _corpus_embedder()

    def tearDown(self):
        self._tmp.cleanup()

    def _titles(self, results):
        return [r["title"] for r in results]


class TestIngestAndSchema(MemoryStoreTestBase):
    def test_ingest_creates_documents_and_chunks(self):
        summary = self.store.ingest_path(str(self.corpus), self.embedder)
        self.assertEqual(summary["documents_new"], 4)
        self.assertEqual(summary["chunks_written"], 4)
        stats = self.store.stats()
        self.assertEqual(stats["total_documents"], 4)
        self.assertEqual(stats["total_chunks"], 4)
        self.assertEqual(stats["by_source_type"]["document"]["documents"], 4)

    def test_chunk_id_format_and_model_recorded(self):
        self.store.ingest_path(str(self.corpus), self.embedder)
        results = self.store.query(self.embedder, Q_PARA, top_k=8)
        self.assertTrue(results)
        for r in results:
            doc_id, seq = r["doc_id"], r["seq"]
            self.assertEqual(r["chunk_id"], f"mem:{doc_id}:{seq}")
            self.assertEqual(r["embedding_model"], "stub-recorded-v1")


class TestHybridRetrieval(MemoryStoreTestBase):
    def setUp(self):
        super().setUp()
        self.store.ingest_path(str(self.corpus), self.embedder)

    def test_paraphrase_served_by_vectors_only(self):
        # Q_PARA shares NO words with any chunk, so BM25 cannot serve it; only
        # the vector signal can, and it must surface CATS on top.  This is
        # asserted purely through the public query() contract (no reaching into
        # _fts_query / chunks_fts internals) — the BM25-only counterpart is
        # proven by test_identifier_served_by_bm25 below.
        results = self.store.query(self.embedder, Q_PARA, top_k=4)
        self.assertTrue(results)
        self.assertEqual(self._titles(results)[0], "cats")

    def test_identifier_served_by_bm25(self):
        # Q_ID embeds to the SAME vector as Q_PARA (feline axis -> nearest CATS),
        # so vectors alone would rank CATS first, NOT the widget. The only
        # difference is the exact-identifier BM25 match -> widget must win.
        id_results = self.store.query(self.embedder, Q_ID, top_k=4)
        self.assertEqual(self._titles(id_results)[0], "widget")

        # Contrast: identical query vector without the lexical id match (Q_PARA)
        # does NOT surface the widget on top -> BM25 was decisive.
        para_results = self.store.query(self.embedder, Q_PARA, top_k=4)
        self.assertEqual(self._titles(para_results)[0], "cats")
        self.assertNotEqual(self._titles(para_results)[0], "widget")

    def test_top_k_respected(self):
        results = self.store.query(self.embedder, Q_PARA, top_k=2)
        self.assertLessEqual(len(results), 2)

    def test_empty_store_returns_no_results(self):
        from memory.store import MemoryStore

        empty = MemoryStore(self.root / "empty.sqlite", embedding_dim=DIM)
        self.assertEqual(empty.query(self.embedder, "anything"), [])


class TestFilters(MemoryStoreTestBase):
    def _ingest_news(self):
        # Two dated news files ingested with distinct published_at values.
        n1 = self.root / "old_news.txt"
        n1.write_text("Older market report about copper prices.", encoding="utf-8")
        n2 = self.root / "new_news.txt"
        n2.write_text("Recent market report about lithium prices.", encoding="utf-8")
        self.store.ingest_path(str(n1), self.embedder, source_type="news", published_at="2024-01-15")
        self.store.ingest_path(str(n2), self.embedder, source_type="news", published_at="2024-11-20")

    def test_source_type_filter(self):
        self.store.ingest_path(str(self.corpus), self.embedder)  # documents
        self._ingest_news()
        results = self.store.query(self.embedder, "market report prices", top_k=10, source_type="news")
        self.assertTrue(results)
        self.assertTrue(all(r["source_type"] == "news" for r in results))

    def test_date_range_filter(self):
        self._ingest_news()
        # Only the January item falls in this window.
        results = self.store.query(
            self.embedder, "market report prices", top_k=10,
            date_from="2024-01-01", date_to="2024-06-30",
        )
        self.assertTrue(results)
        self.assertTrue(all(r["published_at"].startswith("2024-01") for r in results))


class TestDedupAndReplace(MemoryStoreTestBase):
    def test_reingest_unchanged_is_noop(self):
        first = self.store.ingest_path(str(self.corpus), self.embedder)
        self.assertEqual(first["documents_new"], 4)
        second = self.store.ingest_path(str(self.corpus), self.embedder)
        self.assertEqual(second["documents_new"], 0)
        self.assertEqual(second["documents_unchanged"], 4)
        self.assertEqual(second["chunks_written"], 0)

    def test_changed_file_replaces_cleanly(self):
        self.store.ingest_path(str(self.corpus), self.embedder)
        stats_before = self.store.stats()
        # Rewrite one file with new content (new hash) and re-ingest.
        (self.corpus / "cats.txt").write_text(
            "An entirely different sentence about volcanoes and lava.", encoding="utf-8"
        )
        embedder = StubEmbedder(
            {
                "An entirely different sentence about volcanoes and lava.": _e(5),
                DOGS: _e(1), FIN: _e(2), WIDGET: _e(3),
            }
        )
        summary = self.store.ingest_path(str(self.corpus), embedder)
        self.assertEqual(summary["documents_replaced"], 1)
        self.assertEqual(summary["documents_unchanged"], 3)
        # Total chunk count unchanged (clean delete+reinsert, no orphans).
        self.assertEqual(self.store.stats()["total_chunks"], stats_before["total_chunks"])
        # New content is retrievable; old content is gone.
        with self.store._connect() as conn:
            texts = [r["text"] for r in conn.execute("SELECT text FROM chunks").fetchall()]
            vec_count = conn.execute("SELECT COUNT(*) AS n FROM chunks_vec").fetchone()["n"]
            fts_count = conn.execute("SELECT COUNT(*) AS n FROM chunks_fts").fetchone()["n"]
        self.assertTrue(any("volcanoes" in t for t in texts))
        self.assertFalse(any("domesticated feline" in t for t in texts))
        # vec + fts rows stay in lockstep with chunks (no orphans left behind).
        self.assertEqual(vec_count, self.store.stats()["total_chunks"])
        self.assertEqual(fts_count, self.store.stats()["total_chunks"])


class TestWalConcurrency(MemoryStoreTestBase):
    def test_concurrent_ingest_and_query(self):
        # Seed, then hammer the store with simultaneous readers + a writer.
        self.store.ingest_path(str(self.corpus), self.embedder)
        errors = []

        def writer():
            try:
                for i in range(8):
                    f = self.root / f"extra_{i}.txt"
                    f.write_text(f"extra document number {i} about topic {i}.", encoding="utf-8")
                    self.store.ingest_path(str(f), self.embedder)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        def reader():
            try:
                for _ in range(15):
                    self.store.query(self.embedder, Q_PARA, top_k=4)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=writer)] + [
            threading.Thread(target=reader) for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"concurrent access raised: {errors}")


class TestMetadataUpdate(MemoryStoreTestBase):
    def test_metadata_only_change_updates_row_without_reembedding(self):
        # Finding 3: a re-ingest with the SAME content but different metadata
        # (here: relabel document -> news + add published_at) must UPDATE the
        # existing document row, not be silently skipped.  doc_id is path-only
        # so the file maps to one document across labels (finding 14).
        story = self.root / "story.txt"
        story.write_text("Markets rallied today on strong earnings.", encoding="utf-8")
        first = self.store.ingest_path(str(story), self.embedder)
        self.assertEqual(first["documents_new"], 1)

        second = self.store.ingest_path(
            str(story), self.embedder, source_type="news", published_at="2024-05-01"
        )
        self.assertEqual(second["documents_metadata_updated"], 1)
        self.assertEqual(second["documents_new"], 0)
        self.assertEqual(second["documents_replaced"], 0)
        self.assertEqual(second["chunks_written"], 0)  # no re-embed / re-chunk

        # The stored row now reflects the corrected metadata.
        results = self.store.query(
            self.embedder, "markets earnings", top_k=5, source_type="news"
        )
        self.assertTrue(results)
        self.assertTrue(all(r["source_type"] == "news" for r in results))
        self.assertTrue(all(r["published_at"] == "2024-05-01" for r in results))
        # Still exactly one document (no duplicate created by the relabel).
        self.assertEqual(self.store.stats()["total_documents"], 1)


class TestEmptiedFileRemoval(MemoryStoreTestBase):
    def test_emptied_file_reingest_removes_document(self):
        # Finding 7: emptying a previously-ingested file and re-ingesting must
        # REMOVE that document (empty chunk set for a known doc_id = removal),
        # not leave stale content forever.
        self.store.ingest_path(str(self.corpus), self.embedder)
        self.assertEqual(self.store.stats()["total_documents"], 4)

        (self.corpus / "cats.txt").write_text("", encoding="utf-8")
        summary = self.store.ingest_path(str(self.corpus), self.embedder)
        self.assertEqual(summary["documents_removed"], 1)
        self.assertEqual(summary["documents_unchanged"], 3)

        stats = self.store.stats()
        self.assertEqual(stats["total_documents"], 3)
        # The removed content is gone from all three synced tables.
        with self.store._connect() as conn:
            texts = [r["text"] for r in conn.execute("SELECT text FROM chunks").fetchall()]
            vec = conn.execute("SELECT COUNT(*) AS n FROM chunks_vec").fetchone()["n"]
            fts = conn.execute("SELECT COUNT(*) AS n FROM chunks_fts").fetchone()["n"]
        self.assertFalse(any("domesticated feline" in t for t in texts))
        self.assertEqual(vec, stats["total_chunks"])
        self.assertEqual(fts, stats["total_chunks"])

    def test_emptied_unknown_file_is_skipped_not_removed(self):
        empty = self.root / "blank.txt"
        empty.write_text("", encoding="utf-8")
        summary = self.store.ingest_path(str(empty), self.embedder)
        self.assertEqual(summary["documents_removed"], 0)
        self.assertEqual(summary["documents_skipped_empty"], 1)


class _AllSameEmbedder:
    """Every text embeds to the same unit vector (vector signal is uniform)."""

    model_name = "all-same-v1"

    def __init__(self, dim=DIM):
        self._dim = dim
        self._v = _normalize(_e(0))

    @property
    def dim(self):
        return self._dim

    def embed_documents(self, texts):
        return [list(self._v) for _ in texts]

    def embed_query(self, text):
        return list(self._v)


class TestFilteredAtScale(MemoryStoreTestBase):
    def test_source_type_filter_not_starved_by_dominant_type(self):
        # Finding 1: with filters pushed INTO the candidate queries, a
        # source_type='news' query returns the (rare) news even when the corpus
        # is dominated by same-distance documents that would otherwise flood a
        # fixed post-filtered candidate pool.
        emb = _AllSameEmbedder()
        store = self.store
        store.embedding_dim  # sanity: DIM matches
        # 60 documents (all embed to the same vector as the query).
        big = self.root / "big"
        big.mkdir()
        for i in range(60):
            (big / f"doc_{i:03d}.txt").write_text(f"generic filler document body {i}", encoding="utf-8")
        store.ingest_path(str(big), emb)
        # 2 rare news items.
        for i, day in enumerate(("2024-03-01", "2024-09-01")):
            n = self.root / f"news_{i}.txt"
            n.write_text(f"newsy item number {i}", encoding="utf-8")
            store.ingest_path(str(n), emb, source_type="news", published_at=day)

        results = store.query(emb, "some paraphrase query", top_k=5, source_type="news")
        self.assertTrue(results, "news filter starved by dominant documents")
        self.assertTrue(all(r["source_type"] == "news" for r in results))
        self.assertLessEqual(len(results), 2)

    def test_date_range_filter_pushed_down(self):
        emb = _AllSameEmbedder()
        store = self.store
        for i, day in enumerate(("2024-01-10", "2024-06-10", "2024-12-10")):
            n = self.root / f"n{i}.txt"
            n.write_text(f"dated report {i}", encoding="utf-8")
            store.ingest_path(str(n), emb, source_type="news", published_at=day)
        results = store.query(
            emb, "dated report", top_k=10, date_from="2024-05-01", date_to="2024-08-01"
        )
        self.assertTrue(results)
        self.assertTrue(all(r["published_at"].startswith("2024-06") for r in results))


class TestQueryValidation(MemoryStoreTestBase):
    def setUp(self):
        super().setUp()
        self.store.ingest_path(str(self.corpus), self.embedder)

    def test_top_k_must_be_positive(self):
        with self.assertRaises(ValueError):
            self.store.query(self.embedder, Q_PARA, top_k=0)
        with self.assertRaises(ValueError):
            self.store.query(self.embedder, Q_PARA, top_k=-3)

    def test_large_top_k_clamped_not_errored(self):
        # top_k above sqlite-vec's 4096 kNN limit must not raise; the pool clamps.
        results = self.store.query(self.embedder, Q_PARA, top_k=9000)
        self.assertTrue(results)

    def test_dim_mismatch_rejected(self):
        class Wrong(StubEmbedder):
            @property
            def dim(self):
                return DIM + 1

        with self.assertRaises(ValueError):
            self.store.query(Wrong({}), Q_PARA, top_k=4)

    def test_invalid_date_rejected(self):
        with self.assertRaises(ValueError):
            self.store.query(self.embedder, Q_PARA, date_from="7/4/2026")
        with self.assertRaises(ValueError):
            self.store.query(self.embedder, Q_PARA, date_to="2026-13-40")


class TestExistingVecDimGuard(MemoryStoreTestBase):
    def test_reopen_with_wrong_dim_raises(self):
        from memory.store import MemoryStore

        self.store.ingest_path(str(self.corpus), self.embedder)  # builds vec[DIM]
        mismatched = MemoryStore(self.db_path, embedding_dim=DIM + 4)
        with self.assertRaises(ValueError):
            mismatched.stats()  # _ensure_schema detects the dim mismatch on connect


# --------------------------------------------------------------------------- #
# Per-document de-duplication (max_per_doc).  Built on a MULTI-chunk corpus so a
# single document can crowd top_k, which the 4 single-chunk files above cannot
# exercise.  Documents are constructed directly (SourceDocument/ChunkInput) with
# a recorded embedder that places each chunk at a chosen L2 distance from the
# query, giving a deterministic fused order: A0 < A1 < A2 < B0 < C0 < B1.
# --------------------------------------------------------------------------- #
def _dedup_vec(axis: int, offset: float) -> list:
    """Query axis-0 unit + an ``offset`` on a private axis => L2 distance==offset."""
    v = [0.0] * DIM
    v[0] = 1.0
    if axis:
        v[axis] = offset
    return v


# (chunk-text, distance-axis, distance) — distances chosen so the vector rank
# order is exactly the listing order below.
_A0, _A1, _A2 = "alpha zero body", "alpha one body", "alpha two body"
_B0, _B1 = "bravo zero body", "bravo one body"
_C0 = "charlie zero body"
_DEDUP_QUERY = "seeking distinct relevant passages"  # zero lexical overlap w/ chunks


class TestDedupPoolSizing(unittest.TestCase):
    """Regression: the dedup fused-candidate pool must keep BACKFILL headroom that
    scales with top_k.  The original ``max(_DEFAULT_DEDUP_POOL, top_k)`` collapsed
    to exactly top_k once top_k>=50, silently starving the distinct-doc backfill
    (memory_query(top_k=60, max_per_doc=1) returned ~27 docs, not 60).  Pure
    static-method check, so it needs no storage extras."""

    def test_pool_scales_with_top_k_and_is_capped(self):
        from memory.store import MemoryStore, _DEFAULT_DEDUP_POOL, _VEC_K_LIMIT

        # small top_k: at least the floor.
        self.assertGreaterEqual(MemoryStore._resolve_dedup_pool(8), _DEFAULT_DEDUP_POOL)
        # top_k >= 50: pool MUST exceed top_k (the bug was pool == top_k, no backfill).
        for tk in (50, 60, 100, 400):
            self.assertGreater(
                MemoryStore._resolve_dedup_pool(tk), tk,
                f"dedup pool has no backfill headroom at top_k={tk}",
            )
        # pathological top_k: capped at the vec kNN hard limit.
        self.assertEqual(MemoryStore._resolve_dedup_pool(1_000_000), _VEC_K_LIMIT)


@unittest.skipUnless(
    _STORAGE,
    "memory storage extras (sqlite-vec + pysqlite3) not installed",
)
class TestDedupByDoc(unittest.TestCase):
    def setUp(self):
        from memory.store import MemoryStore
        from memory_core.adapters.base import ChunkInput, SourceDocument, sha256_text

        self._tmp = tempfile.TemporaryDirectory(prefix="agent_os_dedup_")
        self.root = Path(self._tmp.name)
        self.store = MemoryStore(self.root / "memory.sqlite", embedding_dim=DIM)

        self.embedder = StubEmbedder(
            {
                _DEDUP_QUERY: _dedup_vec(0, 0.0),  # query -> exact e0
                _A0: _dedup_vec(1, 0.01),
                _A1: _dedup_vec(2, 0.02),
                _A2: _dedup_vec(3, 0.03),
                _B0: _dedup_vec(4, 0.04),
                _C0: _dedup_vec(5, 0.05),
                _B1: _dedup_vec(6, 0.06),
            },
            dim=DIM,
        )
        # StubEmbedder normalizes; that keeps the distance monotonic in the offset
        # (larger offset -> larger angle from e0), so the fused order is preserved.

        def _doc(doc_id, title, texts):
            return SourceDocument(
                doc_id=doc_id, title=title, source_type="document",
                source_path=f"/virtual/{doc_id}.txt",
                content_hash=sha256_text("|".join(texts)),
                provenance="uploaded", published_at=None,
                chunks=[ChunkInput(seq=i, text=t) for i, t in enumerate(texts)],
            )

        docs = [
            _doc("docA", "A", [_A0, _A1, _A2]),
            _doc("docB", "B", [_B0, _B1]),
            _doc("docC", "C", [_C0]),
        ]
        summary = self.store.ingest_documents(docs, self.embedder)
        self.assertEqual(summary["documents_new"], 3)

    def tearDown(self):
        self._tmp.cleanup()

    def _docs(self, results):
        return [r["doc_id"] for r in results]

    def _full_order(self):
        # The whole ranked list (no dedup), as chunk_ids, for order-preservation.
        return [r["chunk_id"] for r in self.store.query(self.embedder, _DEDUP_QUERY, top_k=10)]

    def test_baseline_is_chunk_crowded(self):
        # Without dedup, docA's three chunks fill the whole top-3 (the bug).
        res = self.store.query(self.embedder, _DEDUP_QUERY, top_k=3)
        self.assertEqual(self._docs(res), ["docA", "docA", "docA"])

    def test_default_none_is_byte_identical(self):
        base = self.store.query(self.embedder, _DEDUP_QUERY, top_k=3)
        explicit = self.store.query(self.embedder, _DEDUP_QUERY, top_k=3, max_per_doc=None)
        self.assertEqual(base, explicit)

    def test_max_per_doc_1_backfills_distinct_docs(self):
        res = self.store.query(self.embedder, _DEDUP_QUERY, top_k=3, max_per_doc=1)
        # top_k stays full, now with THREE distinct docs (B0, C0 backfilled from
        # beyond the previously chunk-crowded top-3).
        self.assertEqual(self._docs(res), ["docA", "docB", "docC"])
        self.assertEqual(len(set(self._docs(res))), 3)

    def test_max_per_doc_2_keeps_at_most_two_and_preserves_order(self):
        res = self.store.query(self.embedder, _DEDUP_QUERY, top_k=3, max_per_doc=2)
        self.assertEqual(self._docs(res), ["docA", "docA", "docB"])
        # No doc exceeds the cap.
        for doc_id in set(self._docs(res)):
            self.assertLessEqual(self._docs(res).count(doc_id), 2)

    def test_kept_order_is_a_subsequence_of_full_ranking(self):
        full = self._full_order()
        for m in (1, 2):
            kept = [r["chunk_id"] for r in
                    self.store.query(self.embedder, _DEDUP_QUERY, top_k=6, max_per_doc=m)]
            # Rank order preserved: the kept chunks appear in the same relative
            # order as in the undeduped ranking.
            it = iter(full)
            self.assertTrue(all(cid in it for cid in kept),
                            f"kept={kept} not a subsequence of {full}")

    def test_the_kept_chunk_per_doc_is_the_top_ranked_one(self):
        # max_per_doc=1 keeps the HIGHEST-ranked chunk of each doc (A0, not A1/A2;
        # B0, not B1).
        res = self.store.query(self.embedder, _DEDUP_QUERY, top_k=3, max_per_doc=1)
        by_doc = {r["doc_id"]: r["chunk_id"] for r in res}
        self.assertEqual(by_doc["docA"], "mem:docA:0")
        self.assertEqual(by_doc["docB"], "mem:docB:0")

    def test_no_duplicate_docs_beyond_cap_even_at_large_top_k(self):
        res = self.store.query(self.embedder, _DEDUP_QUERY, top_k=6, max_per_doc=1)
        docs = self._docs(res)
        self.assertEqual(docs, ["docA", "docB", "docC"])  # only 3 distinct docs exist
        self.assertEqual(len(docs), len(set(docs)))

    def test_invalid_max_per_doc_rejected(self):
        for bad in (0, -1, True, "1", 1.5):
            with self.assertRaises(ValueError):
                self.store.query(self.embedder, _DEDUP_QUERY, top_k=3, max_per_doc=bad)

    def test_empty_store_with_dedup_returns_empty(self):
        from memory.store import MemoryStore

        empty = MemoryStore(self.root / "empty.sqlite", embedding_dim=DIM)
        self.assertEqual(empty.query(self.embedder, "anything", max_per_doc=1), [])


if __name__ == "__main__":
    unittest.main()
