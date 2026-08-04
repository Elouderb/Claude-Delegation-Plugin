"""The central memory store: schema, ingest (with dedup/replace), hybrid query.

Follows the card_tools connection discipline — short-lived connections opened by
path with the schema re-ensured on connect — so the machine-global memory.sqlite
self-heals if replaced and tolerates concurrent access from multiple repos'
servers.  Each connection loads the sqlite-vec extension, runs in WAL mode with a
busy timeout, and manages transactions explicitly (autocommit + BEGIN/COMMIT)
so a single ingest can wrap many per-document SAVEPOINTs on one connection.

Storage is split across four tables in one database file (schema user_version 1):
  * documents   — one row per source document, with provenance + dates + hash.
                  source_type is validated in app code (VALID_SOURCE_TYPES), not
                  by a DB CHECK, so new Phase-1 types need no schema rebuild.
  * chunks      — one row per chunk.  ``id INTEGER PRIMARY KEY`` is a true rowid
                  alias (stable across VACUUM / dump-restore); ``chunk_id``
                  (``mem:<doc_id>:<seq>``) is a UNIQUE secondary key.  Embedding
                  model name + dim are recorded per row (migration insurance).
  * chunks_fts  — FTS5 *external-content* index over chunks(text) keyed to
                  chunks.id, kept in DB-enforced lockstep by triggers (one text
                  copy, no hand-sync).
  * chunks_vec  — vec0 virtual table of embeddings keyed to chunks.id.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .adapters import (
    VALID_SOURCE_TYPES,
    SourceDocument,
    detect_and_load,
    validate_iso_date,
)
from .availability import (
    _INSTALL_HINT,
    DEFAULT_EMBEDDING_DIM,
    MemoryUnavailable,
    default_db_path,
    resolve_sqlite_module,
)
from .embeddings import Embedder
from .retrieval import RRF_K, fuse_ranked_ids

# Current on-disk schema version (PRAGMA user_version).  Bump + add a migration
# step in _ensure_schema when the schema changes in a future phase.
_SCHEMA_VERSION = 1

# BM25 candidate-pool / vector-k pool.  Filters are pushed into the candidate
# queries, so the pool only needs to be a modest superset of top_k; it is capped
# at sqlite-vec's hard kNN limit.
_MIN_CANDIDATE_POOL = 50
_CANDIDATE_MULTIPLIER = 5
_VEC_K_LIMIT = 4096
_FTS_MAX_TERMS = 32

_WORD_RE = re.compile(r"\w+")
_VEC_DIM_RE = re.compile(r"FLOAT\[(\d+)\]")


def _import_sqlite_vec():
    try:
        import sqlite_vec

        return sqlite_vec
    except ImportError as exc:
        raise MemoryUnavailable(f"sqlite-vec is not installed. {_INSTALL_HINT}") from exc


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fts_query(text: str) -> Optional[str]:
    """Build a safe FTS5 MATCH expression: quoted terms OR-ed for recall.

    Quoting each term neutralizes FTS5 operators/special characters in user
    input.  Returns None when the query has no indexable terms (skip FTS).
    """
    terms = _WORD_RE.findall(text or "")
    if not terms:
        return None
    terms = terms[:_FTS_MAX_TERMS]
    return " OR ".join(f'"{t}"' for t in terms)


class MemoryStore:
    """Persistence + retrieval engine for the central memory corpus."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    ):
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.embedding_dim = int(embedding_dim)
        self._sqlite_module = None

    # -- connection / schema -------------------------------------------------

    def _sqlite(self):
        if self._sqlite_module is None:
            self._sqlite_module, _ = resolve_sqlite_module()
        return self._sqlite_module

    @contextmanager
    def _connect(self):
        """Yield a fresh, extension-loaded, WAL-mode connection.

        The connection runs in autocommit mode (``isolation_level=None``) with an
        explicit BEGIN/COMMIT around the yielded block, so callers may open inner
        SAVEPOINTs for per-document atomicity; any exception rolls the whole
        block back.
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        sqlite = self._sqlite()
        sqlite_vec = _import_sqlite_vec()
        conn = sqlite.connect(str(self.db_path))
        conn.row_factory = sqlite.Row
        conn.isolation_level = None  # explicit transaction control (see below)
        try:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._ensure_schema(conn)
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def _savepoint(self, conn, name: str):
        """Per-unit atomic scope inside the connection's transaction."""
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")

    def _ensure_schema(self, conn) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        # v1 schema.  IF NOT EXISTS keeps this idempotent on an already-current
        # DB; future phases add `if version < N:` migration steps below.
        self._create_v1(conn)
        if version < _SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def _create_v1(self, conn) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                doc_id TEXT PRIMARY KEY,
                title TEXT,
                source_type TEXT NOT NULL,
                source_path TEXT,
                content_hash TEXT,
                provenance TEXT DEFAULT 'uploaded',
                published_at TEXT,
                ingested_at TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_source_type ON documents(source_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_source_path ON documents(source_path)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY,
                chunk_id TEXT NOT NULL UNIQUE,
                doc_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                text TEXT NOT NULL,
                meta TEXT,
                embedding_model TEXT,
                embedding_dim INTEGER,
                FOREIGN KEY (doc_id) REFERENCES documents(doc_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id)")
        # FTS5 external-content index over chunks(text), keyed to chunks.id.
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
            "text, content='chunks', content_rowid='id', tokenize='porter unicode61')"
        )
        # Triggers keep the FTS index in DB-enforced lockstep with chunks.
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN "
            "INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN "
            "INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text); END"
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN "
            "INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', old.id, old.text); "
            "INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text); END"
        )
        self._ensure_vec_table(conn)

    def _ensure_vec_table(self, conn) -> None:
        """Create chunks_vec at the configured dim, or verify an existing one.

        A dim mismatch means the DB was built with a different embedding model;
        surface a clear diagnostic rather than a later cryptic vec0 MATCH error.
        """
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'chunks_vec'"
        ).fetchone()
        if row is None:
            conn.execute(
                "CREATE VIRTUAL TABLE chunks_vec USING vec0("
                f"chunk_rowid INTEGER PRIMARY KEY, embedding FLOAT[{self.embedding_dim}])"
            )
            return
        match = _VEC_DIM_RE.search(row["sql"] or "")
        existing = int(match.group(1)) if match else None
        if existing is not None and existing != self.embedding_dim:
            raise ValueError(
                f"existing chunks_vec embedding dim {existing} != configured "
                f"{self.embedding_dim}; this database was built with a different "
                "embedding model — use a matching model or a fresh database"
            )

    # -- ingest --------------------------------------------------------------

    def _preload_documents(self, conn) -> Dict[str, dict]:
        """One SELECT of existing document metadata for dedup/metadata compare."""
        rows = conn.execute(
            "SELECT doc_id, title, source_type, source_path, content_hash, "
            "provenance, published_at FROM documents"
        ).fetchall()
        return {r["doc_id"]: dict(r) for r in rows}

    @staticmethod
    def _metadata_differs(prior: dict, doc: SourceDocument) -> bool:
        """True if caller-supplied metadata changed while content stayed the same."""
        return (
            prior.get("title") != doc.title
            or prior.get("source_type") != doc.source_type
            or prior.get("source_path") != doc.source_path
            or prior.get("provenance") != doc.provenance
            or prior.get("published_at") != doc.published_at
        )

    def _delete_document(self, conn, doc_id: str) -> bool:
        """Delete a document and all its chunk/vec rows (FTS synced by trigger)."""
        existed = (
            conn.execute("SELECT 1 FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
            is not None
        )
        conn.execute(
            "DELETE FROM chunks_vec WHERE chunk_rowid IN "
            "(SELECT id FROM chunks WHERE doc_id = ?)",
            (doc_id,),
        )
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
        return existed

    @staticmethod
    def _validate_source_type(doc: SourceDocument) -> None:
        if doc.source_type not in VALID_SOURCE_TYPES:
            raise ValueError(
                f"invalid source_type {doc.source_type!r} for {doc.doc_id}; "
                f"expected one of {VALID_SOURCE_TYPES}"
            )

    def _insert_document(self, conn, doc: SourceDocument, ingested_at: str) -> None:
        self._validate_source_type(doc)
        conn.execute(
            """
            INSERT INTO documents
                (doc_id, title, source_type, source_path, content_hash,
                 provenance, published_at, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc.doc_id,
                doc.title,
                doc.source_type,
                doc.source_path,
                doc.content_hash,
                doc.provenance,
                doc.published_at,
                ingested_at,
            ),
        )

    def _update_document_metadata(self, conn, doc: SourceDocument, ingested_at: str) -> None:
        self._validate_source_type(doc)
        conn.execute(
            """
            UPDATE documents
               SET title = ?, source_type = ?, source_path = ?,
                   provenance = ?, published_at = ?, ingested_at = ?
             WHERE doc_id = ?
            """,
            (
                doc.title,
                doc.source_type,
                doc.source_path,
                doc.provenance,
                doc.published_at,
                ingested_at,
                doc.doc_id,
            ),
        )

    def _insert_chunks(
        self,
        conn,
        doc: SourceDocument,
        vectors: Sequence[Sequence[float]],
        model_name: str,
        dim: int,
    ) -> None:
        """Set-based insert of a document's chunks + vectors (FTS synced by trigger)."""
        sqlite_vec = _import_sqlite_vec()
        next_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM chunks").fetchone()[0] + 1
        chunk_rows = []
        vec_rows = []
        for offset, (chunk, vector) in enumerate(zip(doc.chunks, vectors)):
            rid = next_id + offset
            chunk_rows.append(
                (
                    rid,
                    f"mem:{doc.doc_id}:{chunk.seq}",
                    doc.doc_id,
                    chunk.seq,
                    chunk.text,
                    json.dumps(chunk.meta),
                    model_name,
                    dim,
                )
            )
            vec_rows.append((rid, sqlite_vec.serialize_float32(list(vector))))
        conn.executemany(
            "INSERT INTO chunks (id, chunk_id, doc_id, seq, text, meta, "
            "embedding_model, embedding_dim) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            chunk_rows,
        )
        conn.executemany(
            "INSERT INTO chunks_vec(chunk_rowid, embedding) VALUES (?, ?)",
            vec_rows,
        )

    def ingest_documents(
        self, source_docs: Sequence[SourceDocument], embedder: Embedder
    ) -> dict:
        """Persist SourceDocuments: dedup by hash, replace changes, update metadata.

        * unchanged content + unchanged metadata → no-op (documents_unchanged)
        * unchanged content + changed metadata    → UPDATE the row
          (documents_metadata_updated) — lets a wrong published_at/source_type
          be corrected without re-embedding
        * changed / new content                   → transactional delete+reinsert
          (documents_replaced / documents_new)
        * empty chunk set for a known doc_id       → removal (documents_removed)
        * empty chunk set, unknown doc_id          → skipped (documents_skipped_empty)

        Embeddings for all (re)written documents are computed in a single batched
        call; writes happen on one connection, one SAVEPOINT per document, so a
        single failing document is recorded in ``documents_failed`` without
        aborting the batch or leaving a half-written document.
        """
        if embedder.dim != self.embedding_dim:
            raise ValueError(
                f"embedder dim {embedder.dim} != store dim {self.embedding_dim}; "
                "create the store with embedding_dim=embedder.dim"
            )

        summary = {
            "documents_new": 0,
            "documents_replaced": 0,
            "documents_unchanged": 0,
            "documents_metadata_updated": 0,
            "documents_removed": 0,
            "documents_skipped_empty": 0,
            "documents_failed": [],
            "chunks_written": 0,
            "by_source_type": {},
        }

        # 1) One read of existing document metadata for dedup / metadata compare.
        with self._connect() as conn:
            existing = self._preload_documents(conn)

        # 2) Classify without touching the embedder yet.
        to_write: List[SourceDocument] = []
        to_update: List[SourceDocument] = []
        to_remove: List[SourceDocument] = []
        for doc in source_docs:
            prior = existing.get(doc.doc_id)
            if not doc.chunks:
                if prior is not None:
                    to_remove.append(doc)
                else:
                    summary["documents_skipped_empty"] += 1
                continue
            if prior is not None and prior.get("content_hash") == doc.content_hash:
                if self._metadata_differs(prior, doc):
                    to_update.append(doc)
                else:
                    summary["documents_unchanged"] += 1
                continue
            to_write.append(doc)

        # 3) Batch-embed every chunk of every (re)written document in one call.
        vectors_by_doc = self._embed_documents(to_write, embedder)

        # 4) Write on one connection; one SAVEPOINT per document.
        ingested_at = _now_iso()
        with self._connect() as conn:
            for doc in to_write:
                try:
                    with self._savepoint(conn, "doc"):
                        existed = self._delete_document(conn, doc.doc_id)
                        self._insert_document(conn, doc, ingested_at)
                        self._insert_chunks(
                            conn, doc, vectors_by_doc[doc.doc_id], embedder.model_name, embedder.dim
                        )
                except Exception as exc:  # per-document fault tolerance
                    summary["documents_failed"].append(
                        {"doc_id": doc.doc_id, "source_path": doc.source_path, "error": str(exc)}
                    )
                    continue
                summary["documents_replaced" if existed else "documents_new"] += 1
                summary["chunks_written"] += len(doc.chunks)
                bucket = summary["by_source_type"].setdefault(
                    doc.source_type, {"documents": 0, "chunks": 0}
                )
                bucket["documents"] += 1
                bucket["chunks"] += len(doc.chunks)

            for doc in to_update:
                try:
                    with self._savepoint(conn, "meta"):
                        self._update_document_metadata(conn, doc, ingested_at)
                except Exception as exc:
                    summary["documents_failed"].append(
                        {"doc_id": doc.doc_id, "source_path": doc.source_path, "error": str(exc)}
                    )
                    continue
                summary["documents_metadata_updated"] += 1

            for doc in to_remove:
                try:
                    with self._savepoint(conn, "rm"):
                        self._delete_document(conn, doc.doc_id)
                except Exception as exc:
                    summary["documents_failed"].append(
                        {"doc_id": doc.doc_id, "source_path": doc.source_path, "error": str(exc)}
                    )
                    continue
                summary["documents_removed"] += 1

        return summary

    @staticmethod
    def _embed_documents(
        docs: Sequence[SourceDocument], embedder: Embedder
    ) -> Dict[str, List[List[float]]]:
        """Embed all chunks across ``docs`` in one call, sliced back per doc_id."""
        texts = [chunk.text for doc in docs for chunk in doc.chunks]
        if not texts:
            return {}
        vectors = embedder.embed_documents(texts)
        if len(vectors) != len(texts):
            raise ValueError(
                f"embedder returned {len(vectors)} vectors for {len(texts)} chunks"
            )
        out: Dict[str, List[List[float]]] = {}
        idx = 0
        for doc in docs:
            n = len(doc.chunks)
            out[doc.doc_id] = list(vectors[idx : idx + n])
            idx += n
        return out

    def ingest_path(
        self,
        path: str,
        embedder: Embedder,
        source_type: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> dict:
        """Detect adapters for a file/directory and ingest the resulting documents.

        Per-file parse failures (a malformed .json in a directory) are collected
        into ``files_failed`` and never abort the walk.
        """
        source_docs, file_errors = detect_and_load(
            path, source_type=source_type, published_at=published_at
        )
        summary = self.ingest_documents(source_docs, embedder)
        summary["path"] = str(Path(path).expanduser())
        summary["documents_detected"] = len(source_docs)
        summary["files_failed"] = file_errors
        return summary

    # -- query ---------------------------------------------------------------

    def _candidate_pool(self, top_k: int) -> int:
        return min(max(_MIN_CANDIDATE_POOL, top_k * _CANDIDATE_MULTIPLIER), _VEC_K_LIMIT)

    @staticmethod
    def _doc_filter_sql(
        source_type: Optional[str],
        date_from: Optional[str],
        date_to: Optional[str],
    ):
        """Build a documents-table filter fragment (alias ``d``) + its params."""
        clauses: List[str] = []
        params: List[str] = []
        if source_type:
            clauses.append("d.source_type = ?")
            params.append(source_type)
        if date_from:
            clauses.append(
                "substr(COALESCE(NULLIF(d.published_at, ''), d.ingested_at, ''), 1, 10) >= ?"
            )
            params.append(date_from[:10])
        if date_to:
            clauses.append(
                "substr(COALESCE(NULLIF(d.published_at, ''), d.ingested_at, ''), 1, 10) <= ?"
            )
            params.append(date_to[:10])
        return " AND ".join(clauses), params

    def query(
        self,
        embedder: Embedder,
        query: str,
        top_k: int = 8,
        source_type: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[dict]:
        """Hybrid BM25 + vector kNN retrieval fused with RRF (k=60).

        The source_type / date filters are pushed INTO both candidate queries
        (an FTS join and a vec0 ``chunk_rowid IN (...)`` pre-filter) so filtered
        queries stay correct at corpus scale instead of starving a fixed
        post-filtered pool.  Returns the top_k chunks with document metadata and
        fusion score.
        """
        if embedder.dim != self.embedding_dim:
            raise ValueError(
                f"embedder dim {embedder.dim} != store dim {self.embedding_dim}"
            )
        if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
            raise ValueError(f"top_k must be a positive integer, got {top_k!r}")
        date_from = validate_iso_date(date_from, "date_from")
        date_to = validate_iso_date(date_to, "date_to")
        if source_type is not None and source_type not in VALID_SOURCE_TYPES:
            raise ValueError(
                f"invalid source_type {source_type!r}; expected one of {VALID_SOURCE_TYPES}"
            )

        sqlite_vec = _import_sqlite_vec()
        pool = self._candidate_pool(top_k)
        query_vec = embedder.embed_query(query)
        fts_expr = _fts_query(query)
        filt_sql, filt_params = self._doc_filter_sql(source_type, date_from, date_to)

        with self._connect() as conn:
            if not conn.execute("SELECT EXISTS(SELECT 1 FROM chunks)").fetchone()[0]:
                return []

            vec_inner = (
                "SELECT chunk_rowid, distance FROM chunks_vec "
                "WHERE embedding MATCH ? AND k = ?"
            )
            vec_params: List = [sqlite_vec.serialize_float32(list(query_vec)), pool]
            if filt_sql:
                vec_inner += (
                    " AND chunk_rowid IN (SELECT c.id FROM chunks c "
                    "JOIN documents d ON d.doc_id = c.doc_id WHERE " + filt_sql + ")"
                )
                vec_params += filt_params
            vec_sql = (
                "SELECT c.chunk_id FROM (" + vec_inner + ") v "
                "JOIN chunks c ON c.id = v.chunk_rowid ORDER BY v.distance"
            )
            vec_ids = [r["chunk_id"] for r in conn.execute(vec_sql, vec_params).fetchall()]

            fts_ids: List[str] = []
            if fts_expr:
                fts_sql = (
                    "SELECT c.chunk_id FROM chunks_fts f "
                    "JOIN chunks c ON c.id = f.rowid "
                    "JOIN documents d ON d.doc_id = c.doc_id "
                    "WHERE chunks_fts MATCH ?"
                )
                fts_params: List = [fts_expr]
                if filt_sql:
                    fts_sql += " AND " + filt_sql
                    fts_params += filt_params
                fts_sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
                fts_params.append(pool)
                fts_ids = [r["chunk_id"] for r in conn.execute(fts_sql, fts_params).fetchall()]

            fused = fuse_ranked_ids(vec_ids, fts_ids, top_k=top_k, k=RRF_K)
            rows = self._load_candidate_rows(conn, [cid for cid, _ in fused])

        return [self._result_row(rows[cid], score) for cid, score in fused if cid in rows]

    def _load_candidate_rows(self, conn, chunk_ids) -> Dict[str, dict]:
        rows: Dict[str, dict] = {}
        chunk_ids = list(chunk_ids)
        if not chunk_ids:
            return rows
        # Chunk the IN-list to stay well under SQLite's variable limit.
        for i in range(0, len(chunk_ids), 500):
            batch = chunk_ids[i : i + 500]
            placeholders = ",".join("?" * len(batch))
            for r in conn.execute(
                f"""
                SELECT c.chunk_id, c.text, c.seq, c.meta, c.embedding_model,
                       d.doc_id, d.title, d.source_type, d.published_at,
                       d.ingested_at, d.source_path, d.provenance
                FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
                WHERE c.chunk_id IN ({placeholders})
                """,
                batch,
            ).fetchall():
                rows[r["chunk_id"]] = dict(r)
        return rows

    @staticmethod
    def _result_row(row: dict, score: float) -> dict:
        try:
            meta = json.loads(row.get("meta") or "{}")
        except (TypeError, ValueError):
            meta = {}
        return {
            "chunk_id": row["chunk_id"],
            "text": row["text"],
            "seq": row["seq"],
            "score": round(float(score), 6),
            "doc_id": row["doc_id"],
            "title": row["title"],
            "source_type": row["source_type"],
            "published_at": row["published_at"],
            "ingested_at": row["ingested_at"],
            "source_path": row["source_path"],
            "provenance": row["provenance"],
            "embedding_model": row["embedding_model"],
            "meta": meta,
        }

    # -- status --------------------------------------------------------------

    def stats(self) -> dict:
        """Corpus statistics: document/chunk counts by source_type + model info."""
        with self._connect() as conn:
            doc_rows = conn.execute(
                "SELECT source_type, COUNT(*) AS n FROM documents GROUP BY source_type"
            ).fetchall()
            chunk_rows = conn.execute(
                "SELECT source_type, COUNT(*) AS n "
                "FROM chunks c JOIN documents d ON d.doc_id = c.doc_id "
                "GROUP BY source_type"
            ).fetchall()
            total_docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
            total_chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
            models = [
                r["embedding_model"]
                for r in conn.execute(
                    "SELECT DISTINCT embedding_model FROM chunks "
                    "WHERE embedding_model IS NOT NULL"
                ).fetchall()
            ]
            dims = [
                r["embedding_dim"]
                for r in conn.execute(
                    "SELECT DISTINCT embedding_dim FROM chunks "
                    "WHERE embedding_dim IS NOT NULL"
                ).fetchall()
            ]

        by_source_type: Dict[str, dict] = {}
        for r in doc_rows:
            by_source_type.setdefault(r["source_type"], {"documents": 0, "chunks": 0})[
                "documents"
            ] = r["n"]
        for r in chunk_rows:
            by_source_type.setdefault(r["source_type"], {"documents": 0, "chunks": 0})[
                "chunks"
            ] = r["n"]

        return {
            "total_documents": total_docs,
            "total_chunks": total_chunks,
            "by_source_type": by_source_type,
            "embedding_models": models,
            "embedding_dims": dims,
        }
