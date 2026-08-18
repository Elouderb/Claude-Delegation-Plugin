"""Hermetic tests for scripts/memory_etl_sql_server.py (card 2f1fe295).

NO live SQL Server, NO real corpus. The synthetic local-sqlite fixture (2
documents / 3 chunks, hand-labelled "alpha"/"beta") is built through the REAL
local ``memory.store.MemoryStore`` (the exact schema/serialization the ETL
script reads), so extraction is proven against genuine sqlite-vec-serialized
vectors, not a hand-rolled stand-in. SQL Server itself is replaced by a small
in-memory fake pyodbc cursor/connection (``_FakeCursor``/``_FakeConnection``)
that records every statement, so the LOAD path (identity-preserving INSERT,
content-hash dedup, per-document commit/rollback) is exercised end to end
without a real database — "correct by construction" for the one part that
genuinely needs a live server (the actual ``VECTOR(384)`` write) is covered by
the OPTIONAL, double-gated live test at the bottom (skipped by default,
mirrors ``mcp/tests/test_api_service_live.py``).

Requires the storage extras (sqlite-vec + a loadable-extension-capable
sqlite) to build the fixture; skips cleanly (like ``mcp/tests/test_memory_store.py``)
when they are absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_MCP_ROOT = Path(__file__).resolve().parent.parent
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from memory_core.adapters import ChunkInput, SourceDocument  # noqa: E402
from memory_core.availability import DEFAULT_EMBEDDING_DIM, check_availability  # noqa: E402

import memory_etl_sql_server as etl  # noqa: E402  # pyright: ignore[reportMissingImports]  # scripts/ is on sys.path at runtime (bootstrap above), not statically resolvable

_STORAGE = check_availability()["storage_available"]

DIM = 6  # small dim for fast hermetic fixtures; the live test below uses 384.


def _vec(i: int) -> List[float]:
    """Deterministic, exactly-float32-representable vector for index i."""
    v = [0.0] * DIM
    v[i % DIM] = 1.0
    v[(i + 1) % DIM] = 0.5
    return v


class _FixtureEmbedder:
    """Deterministic embedder: i-th text in a batch -> ``_vec(i)``. No model."""

    def __init__(self, dim: int = DIM, model_name: str = "fixture-embedder-v1"):
        self._dim = dim
        self.model_name = model_name

    @property
    def dim(self) -> int:
        return self._dim

    def embed_documents(self, texts):
        return [_vec(i) if self._dim == DIM else [0.0] * self._dim for i, _ in enumerate(texts)]

    def embed_query(self, text):
        return _vec(0) if self._dim == DIM else [0.0] * self._dim


def _fixture_docs() -> List[SourceDocument]:
    """Two synthetic documents, never real corpus content."""
    return [
        SourceDocument(
            doc_id="doc_alpha",
            title="Alpha Fixture Document",
            source_type="document",
            source_path="/synthetic/alpha.md",
            content_hash="hash-alpha-0001",
            provenance="uploaded",
            published_at="2024-01-01",
            chunks=[
                ChunkInput(seq=0, text="alpha chunk zero", meta={"role": "body"}),
                ChunkInput(seq=1, text="alpha chunk one", meta={"role": "body"}),
            ],
        ),
        SourceDocument(
            doc_id="doc_beta",
            title="Beta Fixture Document",
            source_type="document",
            source_path="/synthetic/beta.md",
            content_hash="hash-beta-0002",
            provenance="uploaded",
            published_at="2024-02-01",
            chunks=[ChunkInput(seq=0, text="beta chunk zero", meta={})],
        ),
    ]


# --------------------------------------------------------------------------- #
# Fake pyodbc-shaped connection/cursor: enough for MemoryStore._preload
# (SELECT ... WHERE document_id/content_hash IN (...)) and this ETL's own
# INSERT/executemany calls. INSERTs into memory.documents are recorded back
# into ``existing_docs`` so a SECOND run() against the SAME instance proves
# idempotency, exactly like a real re-run against a persisted table would.
# --------------------------------------------------------------------------- #
class _FakeCursor:
    _DOC_COLUMNS = (
        "document_id", "title", "source_type", "source_path", "content_hash",
        "source_machine_id", "provenance", "published_at",
    )

    def __init__(self, existing_docs: Optional[Dict[str, dict]] = None):
        self.existing_docs: Dict[str, dict] = dict(existing_docs or {})
        self.executed: List[Tuple[str, tuple]] = []
        self.executemany_calls: List[Tuple[str, list]] = []
        self._rows: List[tuple] = []
        self._desc: List[tuple] = []
        # Test hook: when set, executemany() raises this instead of recording
        # the call, so the privacy-sanitization path (_sanitize_exception) can
        # be exercised end to end through _load without a real driver error.
        self.raise_on_executemany: Optional[Exception] = None

    def execute(self, sql: str, *params):
        self.executed.append((sql, params))
        low = sql.lower()
        if low.startswith("insert into memory.documents"):
            (
                document_id, title, source_type, source_path, content_hash,
                source_machine_id, provenance, published_at, _created_at, _updated_at,
            ) = params
            self.existing_docs[document_id] = {
                "document_id": document_id, "title": title, "source_type": source_type,
                "source_path": source_path, "content_hash": content_hash,
                "source_machine_id": source_machine_id, "provenance": provenance,
                "published_at": published_at,
            }
            self._desc, self._rows = [], []
        elif "select" in low and "document_id in" in low:
            rows = [
                tuple(self.existing_docs[did].get(c) for c in self._DOC_COLUMNS)
                for did in params
                if did in self.existing_docs
            ]
            self._desc = [(c,) for c in self._DOC_COLUMNS]
            self._rows = rows
        elif "select" in low and "content_hash in" in low:
            cols = ("document_id", "content_hash")
            rows = [
                (row["document_id"], row["content_hash"])
                for row in self.existing_docs.values()
                if row.get("content_hash") in params
            ]
            self._desc = [(c,) for c in cols]
            self._rows = rows
        else:
            self._desc, self._rows = [], []
        return self

    def executemany(self, sql: str, rows):
        if self.raise_on_executemany is not None:
            raise self.raise_on_executemany
        self.executemany_calls.append((sql, list(rows)))

    def fetchall(self):
        return self._rows

    @property
    def description(self):
        return self._desc


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@unittest.skipUnless(_STORAGE, "memory storage extras (sqlite-vec + pysqlite3) not installed")
class MemoryEtlTestBase(unittest.TestCase):
    def setUp(self):
        from memory.store import MemoryStore as LocalMemoryStore  # deferred: needs sqlite-vec

        self._tmp = tempfile.TemporaryDirectory(prefix="agent_os_memory_etl_test_")
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "memory.sqlite"
        store = LocalMemoryStore(self.db_path, embedding_dim=DIM)
        self.embedder = _FixtureEmbedder(DIM)
        self.ingest_summary = store.ingest_documents(_fixture_docs(), self.embedder)

    def tearDown(self):
        self._tmp.cleanup()

    def _delete_vector_for(self, doc_id: str) -> None:
        """Delete a document's lone/first chunk's chunks_vec row directly,
        simulating vec/chunks drift, to exercise the missing-vector path."""
        from memory_core.availability import resolve_sqlite_module

        import sqlite_vec

        sqlite_module, _ = resolve_sqlite_module()
        conn = sqlite_module.connect(str(self.db_path))
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        chunk_id = conn.execute(
            "SELECT id FROM chunks WHERE doc_id = ? ORDER BY seq LIMIT 1", (doc_id,)
        ).fetchone()[0]
        conn.execute("DELETE FROM chunks_vec WHERE chunk_rowid = ?", (chunk_id,))
        conn.commit()
        conn.close()

    def _mutate_chunk(self, doc_id: str, seq: int, **columns) -> None:
        """Raw UPDATE of one or more columns on a single ``chunks`` row, to
        simulate local-corpus drift (e.g. inconsistent embedding_dim, a
        corrupted chunk_id). Only touches the plain ``chunks`` table (never
        ``chunks_vec``), so a bare stdlib sqlite3 connection is sufficient —
        no extension loading needed."""
        import sqlite3 as _sqlite3

        set_clause = ", ".join(f"{c} = ?" for c in columns)
        conn = _sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                f"UPDATE chunks SET {set_clause} WHERE doc_id = ? AND seq = ?",
                (*columns.values(), doc_id, seq),
            )
            conn.commit()
        finally:
            conn.close()


class TestExtraction(MemoryEtlTestBase):
    def test_ingest_fixture_sanity(self):
        self.assertEqual(self.ingest_summary["documents_new"], 2)
        self.assertEqual(self.ingest_summary["chunks_written"], 3)

    def test_extract_roundtrips_docs_chunks_and_vectors(self):
        documents, stats = etl.extract(self.db_path)
        self.assertEqual(stats["documents"], 2)
        self.assertEqual(stats["chunks"], 3)
        self.assertEqual(stats["missing_vectors"], 0)
        self.assertEqual(stats["corrupt_vectors"], 0)

        by_id = {d.doc_id: d for d in documents}
        self.assertEqual(set(by_id), {"doc_alpha", "doc_beta"})

        alpha = by_id["doc_alpha"]
        self.assertEqual(alpha.title, "Alpha Fixture Document")
        self.assertEqual(alpha.content_hash, "hash-alpha-0001")
        self.assertEqual(alpha.published_at, "2024-01-01")
        self.assertIsNotNone(alpha.ingested_at)
        self.assertEqual(len(alpha.chunks), 2)

        c0 = alpha.chunks[0]
        self.assertEqual(c0.chunk_id, "mem:doc_alpha:0")
        self.assertEqual(c0.text, "alpha chunk zero")
        self.assertEqual(c0.meta, {"role": "body"})
        self.assertEqual(c0.embedding_model, "fixture-embedder-v1")
        self.assertEqual(c0.embedding_dim, DIM)
        self.assertIsNotNone(c0.vector)
        for expected, got in zip(_vec(0), c0.vector):
            self.assertAlmostEqual(expected, got, places=5)

        c1 = alpha.chunks[1]
        self.assertEqual(c1.chunk_id, "mem:doc_alpha:1")
        for expected, got in zip(_vec(1), c1.vector):
            self.assertAlmostEqual(expected, got, places=5)

        beta = by_id["doc_beta"]
        self.assertEqual(len(beta.chunks), 1)
        self.assertEqual(beta.chunks[0].chunk_id, "mem:doc_beta:0")
        for expected, got in zip(_vec(2), beta.chunks[0].vector):
            self.assertAlmostEqual(expected, got, places=5)

    def test_missing_vector_chunk_is_reported_and_excluded(self):
        self._delete_vector_for("doc_beta")
        documents, stats = etl.extract(self.db_path)
        self.assertEqual(stats["missing_vectors"], 1)
        self.assertEqual(stats["corrupt_vectors"], 0)
        by_id = {d.doc_id: d for d in documents}
        self.assertEqual(len(by_id["doc_beta"].chunks), 1)
        self.assertIsNone(by_id["doc_beta"].chunks[0].vector)

    def test_uncheckpointed_wal_write_is_captured_by_backup(self):
        """backup() must capture committed-but-not-yet-checkpointed WAL
        content — the exact race the rejected multi-file-copy approach could
        silently lose (a concurrent checkpoint completing between copying the
        main file and the ``-wal`` sidecar). A second connection, opened
        BEFORE the write and held open throughout, prevents SQLite's
        auto-checkpoint-and-delete-WAL-on-last-close (the store's own writer
        connection closes after every operation via its `_connect()` context
        manager, but that only checkpoints the WAL when it is the LAST open
        connection to the file) — so at the moment extract() runs, the
        just-ingested chunk is genuinely sitting ONLY in ``-wal``, not yet
        merged into the main db file. A precondition assertion confirms the
        ``-wal`` file is actually non-empty at that moment, so this test can't
        silently pass without exercising the race at all.
        """
        from memory.store import MemoryStore as LocalMemoryStore
        from memory_core.availability import resolve_sqlite_module

        sqlite_module, _ = resolve_sqlite_module()
        keepalive = sqlite_module.connect(str(self.db_path))
        keepalive.execute("PRAGMA journal_mode=WAL")
        try:
            store = LocalMemoryStore(self.db_path, embedding_dim=DIM)
            gamma = SourceDocument(
                doc_id="doc_gamma",
                title="Gamma Fixture Document (uncheckpointed)",
                source_type="document",
                source_path="/synthetic/gamma.md",
                content_hash="hash-gamma-0003",
                provenance="uploaded",
                published_at="2024-03-01",
                chunks=[ChunkInput(seq=0, text="gamma chunk zero", meta={})],
            )
            store.ingest_documents([gamma], _FixtureEmbedder(DIM))

            wal_path = self.db_path.with_name(self.db_path.name + "-wal")
            self.assertTrue(
                wal_path.exists() and wal_path.stat().st_size > 0,
                "precondition failed: the write must still be sitting only in "
                "-wal (unchecked) for this test to actually exercise the race "
                "backup() has to handle",
            )

            documents, stats = etl.extract(self.db_path)
        finally:
            keepalive.close()

        self.assertEqual(stats["documents"], 3)  # alpha, beta, gamma
        self.assertEqual(stats["chunks"], 4)  # 2 + 1 + 1
        by_id = {d.doc_id: d for d in documents}
        self.assertIn("doc_gamma", by_id)
        gamma_out = by_id["doc_gamma"]
        self.assertEqual(len(gamma_out.chunks), 1)
        self.assertEqual(gamma_out.chunks[0].chunk_id, "mem:doc_gamma:0")
        self.assertIsNotNone(gamma_out.chunks[0].vector)
        # gamma is ingested in its OWN ingest_documents([gamma], ...) call, a
        # separate batch from the alpha/beta fixture, so its text is index 0
        # within THAT call's embed_documents texts list -> _vec(0), same as
        # alpha's first chunk (a coincidence of the deterministic stub, not a
        # collision of anything that matters here).
        for expected, got in zip(_vec(0), gamma_out.chunks[0].vector):
            self.assertAlmostEqual(expected, got, places=5)


class TestDimReport(MemoryEtlTestBase):
    def test_dim_report_all_consistent(self):
        documents, _ = etl.extract(self.db_path)
        report = etl._dim_report(documents, DIM)
        self.assertEqual(report["documents"], 2)
        self.assertEqual(report["usable_chunks"], 3)
        self.assertEqual(report["embedding_dims_seen"], [DIM])
        self.assertEqual(report["chunks_with_dim_mismatch"], 0)
        self.assertEqual(report["chunk_count_min"], 1)
        self.assertEqual(report["chunk_count_max"], 2)
        self.assertEqual(len(report["sample_documents"]), 2)

    def test_dim_report_flags_mismatch_against_expected_dim(self):
        documents, _ = etl.extract(self.db_path)
        report = etl._dim_report(documents, DIM + 1)
        self.assertEqual(report["chunks_with_dim_mismatch"], 3)


class TestDryRun(MemoryEtlTestBase):
    def test_dry_run_reports_and_writes_nothing(self):
        existing = {
            "doc_beta_prior": {
                "document_id": "doc_beta_prior", "title": "prior", "source_type": "document",
                "source_path": None, "content_hash": "hash-beta-0002",
                "source_machine_id": "mach_prior", "provenance": "etl", "published_at": None,
            }
        }
        cursor = _FakeCursor(existing_docs=existing)
        conn = _FakeConnection(cursor)

        result = etl.run(
            self.db_path, dry_run=True, embedding_dim=DIM,
            source_machine_id="mach_test", connect=lambda: conn,
        )

        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(result["extract"]["documents"], 2)
        self.assertEqual(result["extract"]["chunks"], 3)
        self.assertEqual(result["report"]["would_skip_existing"], 1)  # doc_beta, hash collides
        self.assertEqual(result["report"]["would_fail"], 0)
        self.assertEqual(result["report"]["would_insert"], 1)  # doc_alpha
        self.assertEqual(result["report"]["expected_dim"], DIM)

        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 0)
        self.assertEqual(cursor.executemany_calls, [])
        self.assertFalse(
            any(sql.lower().startswith("insert") for sql, _ in cursor.executed)
        )

    def test_dry_run_would_fail_accounts_for_invalid_documents(self):
        """would_insert must subtract BOTH would-skip and would-fail — a doc
        that would fail validation (here: a corrupted chunk_id) is neither
        skipped nor insertable, so the preview must not count it as either."""
        self._mutate_chunk("doc_beta", 0, chunk_id="mem:doc_beta:not-the-right-seq")
        cursor = _FakeCursor()
        conn = _FakeConnection(cursor)

        result = etl.run(
            self.db_path, dry_run=True, embedding_dim=DIM,
            source_machine_id="mach_test", connect=lambda: conn,
        )

        report = result["report"]
        self.assertEqual(report["would_skip_existing"], 0)
        self.assertEqual(report["would_fail"], 1)  # doc_beta
        self.assertEqual(report["would_insert"], 1)  # doc_alpha; 2 total - 0 skip - 1 fail
        self.assertEqual(conn.commits, 0)
        self.assertEqual(cursor.executemany_calls, [])


class TestLoad(MemoryEtlTestBase):
    def test_insert_new_and_skip_existing_by_content_hash(self):
        existing = {
            "doc_beta_prior_run": {
                "document_id": "doc_beta_prior_run", "title": "x", "source_type": "document",
                "source_path": None, "content_hash": "hash-beta-0002",  # same hash as doc_beta
                "source_machine_id": "mach_other", "provenance": "etl", "published_at": None,
            }
        }
        cursor = _FakeCursor(existing_docs=existing)
        conn = _FakeConnection(cursor)

        result = etl.run(
            self.db_path, dry_run=False, embedding_dim=DIM,
            source_machine_id="mach_test", connect=lambda: conn,
        )

        self.assertEqual(result["mode"], "load")
        self.assertEqual(result["documents_inserted"], 1)  # doc_alpha only
        self.assertEqual(result["documents_skipped"], 1)  # doc_beta, hash collides
        self.assertEqual(result["documents_failed"], [])
        self.assertEqual(result["chunks_inserted"], 2)
        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.rollbacks, 0)

        doc_inserts = [
            (sql, params) for sql, params in cursor.executed
            if sql.lower().startswith("insert into memory.documents")
        ]
        self.assertEqual(len(doc_inserts), 1)
        _sql, params = doc_inserts[0]
        self.assertEqual(params[0], "doc_alpha")  # document_id VERBATIM
        self.assertEqual(params[5], "mach_test")  # source_machine_id
        self.assertEqual(params[6], "etl")  # provenance forced, not the local row's "uploaded"
        self.assertEqual(params[7], "2024-01-01")  # published_at preserved
        self.assertIsNotNone(params[8])  # created_at <- ingested_at
        self.assertEqual(params[8], params[9])  # created_at == updated_at

        self.assertEqual(len(cursor.executemany_calls), 1)
        chunks_sql, chunks_rows = cursor.executemany_calls[0]
        self.assertIn(f"VECTOR({DIM})", chunks_sql)
        self.assertEqual(len(chunks_rows), 2)
        self.assertEqual(chunks_rows[0][0], "mem:doc_alpha:0")  # chunk_id VERBATIM
        self.assertEqual(chunks_rows[0][1], "doc_alpha")
        self.assertEqual(chunks_rows[0][5], "fixture-embedder-v1")  # embedding_model from source
        self.assertEqual(chunks_rows[0][6], DIM)  # embedding_dim from source
        decoded = json.loads(chunks_rows[0][7])
        for expected, got in zip(_vec(0), decoded):
            self.assertAlmostEqual(expected, got, places=5)

    def test_rerun_is_idempotent(self):
        cursor = _FakeCursor()
        conn = _FakeConnection(cursor)
        connect = lambda: conn  # noqa: E731

        first = etl.run(
            self.db_path, dry_run=False, embedding_dim=DIM,
            source_machine_id="mach_test", connect=connect,
        )
        self.assertEqual(first["documents_inserted"], 2)
        self.assertEqual(first["documents_skipped"], 0)
        self.assertEqual(first["documents_failed"], [])

        second = etl.run(
            self.db_path, dry_run=False, embedding_dim=DIM,
            source_machine_id="mach_test", connect=connect,
        )
        self.assertEqual(second["documents_inserted"], 0)
        self.assertEqual(second["documents_skipped"], 2)
        self.assertEqual(second["documents_failed"], [])

    def test_document_with_no_usable_vector_is_failed_not_crashed(self):
        self._delete_vector_for("doc_beta")
        cursor = _FakeCursor()
        conn = _FakeConnection(cursor)

        result = etl.run(
            self.db_path, dry_run=False, embedding_dim=DIM,
            source_machine_id="mach_test", connect=lambda: conn,
        )

        self.assertEqual(result["documents_inserted"], 1)  # doc_alpha
        self.assertEqual(len(result["documents_failed"]), 1)
        self.assertEqual(result["documents_failed"][0]["doc_id"], "doc_beta")
        self.assertEqual(conn.commits, 1)  # only alpha committed

    def test_inconsistent_embedding_model_across_chunks_is_failed(self):
        # doc_alpha's two chunks start with the SAME embedding_model; drift one
        # of them (a pure metadata field — this does NOT touch embedding_dim
        # or the chunks_vec blob, so the vector itself still unpacks cleanly;
        # only the model-name consistency check should fire) so the document
        # can never be loaded as a single, coherently-attributed VECTOR(n)
        # batch.
        self._mutate_chunk("doc_alpha", 1, embedding_model="a-different-model-v2")
        cursor = _FakeCursor()
        conn = _FakeConnection(cursor)

        result = etl.run(
            self.db_path, dry_run=False, embedding_dim=DIM,
            source_machine_id="mach_test", connect=lambda: conn,
        )

        self.assertEqual(result["documents_inserted"], 1)  # doc_beta only
        self.assertEqual(len(result["documents_failed"]), 1)
        self.assertEqual(result["documents_failed"][0]["doc_id"], "doc_alpha")
        self.assertIn(
            "inconsistent embedding_model/embedding_dim", result["documents_failed"][0]["error"]
        )
        self.assertEqual(conn.commits, 1)  # only beta committed; alpha never attempted

    def test_chunk_id_not_matching_mem_doc_seq_is_failed(self):
        self._mutate_chunk("doc_beta", 0, chunk_id="mem:doc_beta:not-the-right-seq")
        cursor = _FakeCursor()
        conn = _FakeConnection(cursor)

        result = etl.run(
            self.db_path, dry_run=False, embedding_dim=DIM,
            source_machine_id="mach_test", connect=lambda: conn,
        )

        self.assertEqual(result["documents_inserted"], 1)  # doc_alpha only
        self.assertEqual(len(result["documents_failed"]), 1)
        self.assertEqual(result["documents_failed"][0]["doc_id"], "doc_beta")
        self.assertIn("chunk_id does not match", result["documents_failed"][0]["error"])
        self.assertEqual(conn.commits, 1)  # only alpha committed; beta never attempted

    def test_embedding_dim_mismatch_vs_store_is_precheck_failed_before_insert(self):
        """The fixture's chunks all carry embedding_dim=DIM, but the store here
        is configured for a DIFFERENT width (DIM + 1, e.g. the operator passed
        the wrong --embedding-dim). This must be caught by _validate_document
        BEFORE any INSERT/executemany is attempted, not surface late as a raw
        CAST(... AS VECTOR(n)) driver exception."""
        cursor = _FakeCursor()
        conn = _FakeConnection(cursor)

        result = etl.run(
            self.db_path, dry_run=False, embedding_dim=DIM + 1,
            source_machine_id="mach_test", connect=lambda: conn,
        )

        self.assertEqual(result["documents_inserted"], 0)
        self.assertEqual(len(result["documents_failed"]), 2)  # both alpha and beta
        for failure in result["documents_failed"]:
            self.assertIn("embedding_dim does not match", failure["error"])
        self.assertEqual(conn.commits, 0)
        self.assertEqual(cursor.executemany_calls, [])
        self.assertFalse(
            any(sql.lower().startswith("insert") for sql, _ in cursor.executed)
        )

    def test_insert_time_exception_is_sanitized_not_leaked_verbatim(self):
        """A raw driver exception (which can echo bound parameter values, e.g.
        a SQL Server truncation error quoting the offending string) must never
        reach documents_failed verbatim — only a short, corpus-safe reason."""
        cursor = _FakeCursor()
        cursor.raise_on_executemany = RuntimeError(
            "driver message that would echo corpus row content 'do not leak me'"
        )
        conn = _FakeConnection(cursor)

        result = etl.run(
            self.db_path, dry_run=False, embedding_dim=DIM,
            source_machine_id="mach_test", connect=lambda: conn,
        )

        self.assertEqual(result["documents_inserted"], 0)
        self.assertEqual(len(result["documents_failed"]), 2)
        for failure in result["documents_failed"]:
            self.assertEqual(failure["error"], "RuntimeError")
            self.assertNotIn("do not leak me", failure["error"])
        self.assertEqual(conn.rollbacks, 2)
        self.assertEqual(conn.commits, 0)


class TestSanitizeException(unittest.TestCase):
    def test_pyodbc_style_error_reports_type_and_sqlstate_only(self):
        # pyodbc's own error classes carry (sqlstate, message) as .args; a
        # plain exception with the same shape exercises the same branch.
        exc = ValueError("22001", "String or binary data would be truncated: 'super secret text'")
        result = etl._sanitize_exception(exc)
        self.assertEqual(result, "ValueError (22001)")
        self.assertNotIn("secret", result)
        self.assertNotIn("truncated", result)

    def test_generic_exception_reports_type_only(self):
        exc = RuntimeError("some very specific message with row content")
        result = etl._sanitize_exception(exc)
        self.assertEqual(result, "RuntimeError")
        self.assertNotIn("row content", result)

    def test_long_first_arg_is_not_mistaken_for_a_sqlstate_code(self):
        # A plain single-string exception (the common case) must not have its
        # whole message echoed just because args[0] happens to be a string.
        exc = RuntimeError("this is a normal long human-readable error message")
        result = etl._sanitize_exception(exc)
        self.assertEqual(result, "RuntimeError")


class TestArgParsing(unittest.TestCase):
    def test_defaults(self):
        args = etl._build_arg_parser().parse_args([])
        self.assertIsNone(args.source)
        self.assertFalse(args.dry_run)
        self.assertEqual(args.embedding_dim, DEFAULT_EMBEDDING_DIM)

    def test_dry_run_and_source_flags(self):
        args = etl._build_arg_parser().parse_args(
            ["--dry-run", "--source", "/tmp/x.sqlite", "--embedding-dim", "8"]
        )
        self.assertTrue(args.dry_run)
        self.assertEqual(args.source, Path("/tmp/x.sqlite"))
        self.assertEqual(args.embedding_dim, 8)


# --------------------------------------------------------------------------- #
# OPTIONAL live round trip against the REAL central SQL Server. Double-gated
# (mirrors mcp/tests/test_api_service_live.py) — skipped by default. Writes
# exactly one clearly-synthetic test document (random id + hash, never real
# corpus data) and deletes it in tearDown, leaving the shared memory schema
# exactly as found. This test is never invoked by the ETL's author/reviewer —
# running it against the live database is the lead/user's deliberate,
# gated step.
# --------------------------------------------------------------------------- #
_HAS_CENTRAL_DB = bool(os.environ.get("AGENT_OS_CENTRAL_DB_CONNECTION_STRING")) and (
    os.environ.get("AGENT_OS_RUN_LIVE_DB_TESTS") == "1"
)


@unittest.skipUnless(_STORAGE, "memory storage extras (sqlite-vec + pysqlite3) not installed")
@unittest.skipUnless(
    _HAS_CENTRAL_DB,
    "live ETL load test requires AGENT_OS_CENTRAL_DB_CONNECTION_STRING + AGENT_OS_RUN_LIVE_DB_TESTS=1",
)
class MemoryEtlLiveLoadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from database import migrate as db_migrate

        conn = db_migrate.connect(timeout=30)
        try:
            db_migrate.apply_pending(db_migrate.DEFAULT_MIGRATIONS_DIR, conn)
        finally:
            conn.close()

    def setUp(self):
        from memory.store import MemoryStore as LocalMemoryStore

        self._tmp = tempfile.TemporaryDirectory(prefix="agent_os_memory_etl_live_")
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "memory.sqlite"
        self.doc_id = f"etl_live_test_{uuid.uuid4().hex[:12]}"
        self.content_hash = hashlib.sha256(self.doc_id.encode("utf-8")).hexdigest()

        store = LocalMemoryStore(self.db_path, embedding_dim=DEFAULT_EMBEDDING_DIM)
        embedder = _FixtureEmbedder(DEFAULT_EMBEDDING_DIM, model_name="live-fixture-embedder-v1")
        doc = SourceDocument(
            doc_id=self.doc_id,
            title="ETL live smoke test document (synthetic, safe to delete)",
            source_type="document",
            source_path=None,
            content_hash=self.content_hash,
            provenance="uploaded",
            published_at=None,
            chunks=[ChunkInput(seq=0, text="synthetic etl live test chunk", meta={})],
        )
        store.ingest_documents([doc], embedder)

    def tearDown(self):
        from database import migrate as db_migrate

        conn = db_migrate.connect(timeout=30)
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM memory.documents WHERE document_id = ?", self.doc_id)
            conn.commit()
        finally:
            conn.close()
        self._tmp.cleanup()

    def test_load_round_trip_against_live_sql_server(self):
        from contracts import ensure_machine_identity
        from database import migrate as db_migrate

        identity = ensure_machine_identity()
        result = etl.run(
            self.db_path, dry_run=False, embedding_dim=DEFAULT_EMBEDDING_DIM,
            source_machine_id=identity.machine_id, connect=db_migrate.connect,
        )
        self.assertEqual(result["documents_inserted"], 1)
        self.assertEqual(result["chunks_inserted"], 1)
        self.assertEqual(result["documents_failed"], [])

        conn = db_migrate.connect(timeout=30)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT provenance, source_machine_id FROM memory.documents WHERE document_id = ?",
                self.doc_id,
            )
            row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        self.assertEqual(row[0], "etl")
        self.assertEqual(row[1], identity.machine_id)


if __name__ == "__main__":
    unittest.main()
