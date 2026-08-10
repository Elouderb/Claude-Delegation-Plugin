"""Central-memory persistence for the Agent OS API — the hub-side ingest store.

This is the SQL-Server-backed WRITE half of central memory (epic 6fead93b,
Slice 1c). It mirrors two established patterns:

* **services/api/store.py** — the same two-implementation shape (a SQL-backed
  :class:`MemoryStore` with per-operation connection discipline, plus an
  in-memory :class:`FakeMemoryStore` parity double so the endpoint tests run with
  NO database). The SQL store opens ONE short-lived connection per method call via
  the injected ``connect`` callable (normally :func:`database.migrate.connect`,
  which registers the DATETIMEOFFSET output converter), does its parameterized
  work in one transaction, commits, and closes — exactly like :class:`SqlStore`.

* **mcp/memory/store.py** — the LOCAL (sqlite + sqlite-vec) MemoryStore's
  ``ingest_documents`` BEHAVIOR: the same 3-way classify (unchanged /
  metadata-changed / content-changed→replace), the same content-hash dedup, the
  same replace-on-change delete+reinsert, and the same summary shape
  (``documents_new`` / ``documents_replaced`` / ``documents_unchanged`` /
  ``documents_metadata_updated`` / ``documents_removed`` /
  ``documents_skipped_empty`` / ``documents_failed`` / ``chunks_written`` /
  ``by_source_type``). The classify/embed/summary primitives are module-level and
  SHARED by both implementations so the SQL store and its fake double cannot
  drift.

Two deliberate additions over the local store, both required by migration 008 and
the epic's lead resolutions:

* **Provenance.** ``memory.documents.source_machine_id`` records which machine's
  ingest produced/last-touched a row (no FK — a document may arrive from a
  machine that never registers). It is a per-ingest value (all documents in one
  ``ingest_documents`` call came from the same caller), so it is a keyword
  argument, not a per-``SourceDocument`` field.

* **Global content-hash dedup.** ``UQ_memory_documents_content_hash`` makes the
  content hash globally unique (stricter than local's per-document-id dedup). A
  NEW identity whose content already lives under a DIFFERENT ``document_id`` is
  reconciled to the existing row (counted as ``documents_unchanged``) rather than
  duplicated — detected PROACTIVELY from a preloaded ``content_hash → document_id``
  map, with the SQL store additionally catching a ``pyodbc.IntegrityError`` as the
  concurrency safety net (mirroring :meth:`SqlStore.upsert_repository`).

VECTOR marshaling is the confirmed migration-008 shape (card c8e686a9): the
embedding is inserted as ``CAST(? AS VECTOR(384))`` with the bound parameter =
``json.dumps(embedding_floats)`` (a single cast; ODBC Driver 17 verified).
``chunk_id`` is ``mem:<document_id>:<seq>``, byte-identical to the local scheme.

Applying migration 008 / connecting to the live SQL Server is out of scope here
(lead-gated): the hermetic tests exercise :class:`FakeMemoryStore` + a
deterministic fake embedder and never touch SQL Server.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from memory_core.adapters import SourceDocument
from memory_core.availability import DEFAULT_EMBEDDING_DIM
from memory_core.embeddings import Embedder

# Cap the IN-list size for the scoped preload so we stay well under SQL Server's
# parameter limit (2100); document batches are tiny in practice.
_IN_CHUNK = 500


# --------------------------------------------------------------------------- #
# Shared, storage-agnostic primitives (used by BOTH implementations)
# --------------------------------------------------------------------------- #
def _new_summary() -> dict:
    """A fresh ingest summary, shape-identical to the local MemoryStore's."""
    return {
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


def _metadata_differs(
    prior: dict, doc: SourceDocument, source_machine_id: Optional[str]
) -> bool:
    """True if caller-supplied metadata changed while content stayed the same.

    Mirrors the local store's comparison (title / source_type / source_path /
    provenance / published_at) plus ``source_machine_id`` — a re-ingest of
    identical content from a different machine refreshes provenance without
    re-embedding.
    """
    return (
        prior.get("title") != doc.title
        or prior.get("source_type") != doc.source_type
        or prior.get("source_path") != doc.source_path
        or prior.get("provenance") != doc.provenance
        or prior.get("published_at") != doc.published_at
        or prior.get("source_machine_id") != source_machine_id
    )


@dataclass
class _Plan:
    """The outcome of classifying a batch: what to write / update / remove."""

    to_write: List[SourceDocument] = field(default_factory=list)
    to_update: List[SourceDocument] = field(default_factory=list)
    to_remove: List[SourceDocument] = field(default_factory=list)
    unchanged: int = 0
    skipped_empty: int = 0
    reconciled: int = 0  # cross-identity content-hash dups (reported as unchanged)


def _classify(
    source_docs: Sequence[SourceDocument],
    existing: Dict[str, dict],
    hash_owner: Dict[str, str],
    source_machine_id: Optional[str],
) -> _Plan:
    """Split a batch into write/update/remove without touching the embedder.

    Mirrors the local store's per-``document_id`` classify, then adds the global
    content-hash dedup: a document that would be WRITTEN with a content hash owned
    by a DIFFERENT ``document_id`` is reconciled (skipped) rather than inserted —
    the ``UQ_memory_documents_content_hash`` index would reject the duplicate. The
    in-batch ``seen_hashes`` map lets a later document in the SAME batch reconcile
    against an earlier one too.
    """
    plan = _Plan()
    seen_hashes = dict(hash_owner)  # updated as we admit writes (in-batch dedup)
    for doc in source_docs:
        prior = existing.get(doc.doc_id)
        if not doc.chunks:
            if prior is not None:
                plan.to_remove.append(doc)
            else:
                plan.skipped_empty += 1
            continue
        if prior is not None and prior.get("content_hash") == doc.content_hash:
            if _metadata_differs(prior, doc, source_machine_id):
                plan.to_update.append(doc)
            else:
                plan.unchanged += 1
            continue
        owner = seen_hashes.get(doc.content_hash)
        if owner is not None and owner != doc.doc_id:
            # Same content already present under a DIFFERENT identity: reconcile to
            # the existing row instead of writing a duplicate (content-addressed
            # global dedup, backed by UQ_memory_documents_content_hash). This runs
            # AFTER the same-identity checks above, so it also covers a REPLACE
            # attempt whose NEW content happens to collide with another existing
            # document: that replace is intentionally folded into documents_unchanged
            # (the pre-existing owner row wins; nothing is written or overwritten) —
            # a direct consequence of the global content-hash UNIQUE constraint,
            # which has no equivalent in the local per-doc-id store.
            plan.reconciled += 1
            continue
        plan.to_write.append(doc)
        seen_hashes[doc.content_hash] = doc.doc_id
    return plan


def _embed_documents(
    docs: Sequence[SourceDocument], embedder: Embedder
) -> Dict[str, List[List[float]]]:
    """Embed every chunk across ``docs`` in ONE call, sliced back per document.

    Mirrors the local store's single batched embed call, so a multi-document
    ingest pays one model round trip.
    """
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
        out[doc.doc_id] = [list(v) for v in vectors[idx : idx + n]]
        idx += n
    return out


def _bucket(summary: dict, doc: SourceDocument) -> None:
    """Tally one written document into ``summary['by_source_type']``."""
    bucket = summary["by_source_type"].setdefault(
        doc.source_type, {"documents": 0, "chunks": 0}
    )
    bucket["documents"] += 1
    bucket["chunks"] += len(doc.chunks)


def _chunked(items: List[str], size: int = _IN_CHUNK) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# --------------------------------------------------------------------------- #
# SQL-backed store (production)
# --------------------------------------------------------------------------- #
class MemoryStore:
    """SQL-Server-backed central memory store. One connection per method call."""

    def __init__(
        self,
        connect: Callable[..., Any],
        embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    ):
        """``connect`` is a zero-arg callable returning a pyodbc connection with the
        DATETIMEOFFSET converter registered — normally ``database.migrate.connect``.
        ``embedding_dim`` must match the VECTOR(n) column in migration 008.
        """
        self._connect = connect
        self.embedding_dim = int(embedding_dim)

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _integrity_error() -> "type[BaseException]":
        import pyodbc  # local import: only needed when a driver error is caught

        return pyodbc.IntegrityError

    @staticmethod
    def _rows(cur) -> List[Dict[str, Any]]:
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def _preload(
        self, cur, doc_ids: List[str], content_hashes: List[str]
    ) -> tuple[Dict[str, dict], Dict[str, str]]:
        """Load only the documents relevant to this batch (by id OR content hash).

        Scoped (unlike the local store's full-table preload) so a single-document
        ingest against a large corpus reads a couple of rows, not the whole table.
        Returns ``(existing_by_id, content_hash → document_id)``.
        """
        existing: Dict[str, dict] = {}
        hash_owner: Dict[str, str] = {}
        ids = sorted({d for d in doc_ids if d})
        hashes = sorted({h for h in content_hashes if h})
        cols = (
            "document_id, title, source_type, source_path, content_hash, "
            "source_machine_id, provenance, published_at"
        )
        for batch in _chunked(ids):
            placeholders = ",".join("?" * len(batch))
            cur.execute(
                f"SELECT {cols} FROM memory.documents "
                f"WHERE document_id IN ({placeholders})",
                *batch,
            )
            for row in self._rows(cur):
                existing[row["document_id"]] = row
                if row.get("content_hash"):
                    hash_owner[row["content_hash"]] = row["document_id"]
        for batch in _chunked(hashes):
            placeholders = ",".join("?" * len(batch))
            cur.execute(
                "SELECT document_id, content_hash FROM memory.documents "
                f"WHERE content_hash IN ({placeholders})",
                *batch,
            )
            for row in self._rows(cur):
                if row.get("content_hash"):
                    hash_owner[row["content_hash"]] = row["document_id"]
        return existing, hash_owner

    def _delete_document(self, cur, document_id: str) -> bool:
        """Delete a document; its chunks go via the FK ON DELETE CASCADE.

        Returns whether the row existed (drives new-vs-replaced accounting).
        """
        cur.execute(
            "SELECT 1 FROM memory.documents WHERE document_id = ?", document_id
        )
        existed = cur.fetchone() is not None
        # memory.chunks -> memory.documents is ON DELETE CASCADE (migration 008),
        # so deleting the document row removes its chunk rows atomically.
        cur.execute("DELETE FROM memory.documents WHERE document_id = ?", document_id)
        return existed

    def _insert_document(
        self, cur, doc: SourceDocument, source_machine_id: Optional[str]
    ) -> None:
        cur.execute(
            "INSERT INTO memory.documents "
            "(document_id, title, source_type, source_path, content_hash, "
            "source_machine_id, provenance, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            doc.doc_id,
            doc.title,
            doc.source_type,
            doc.source_path,
            doc.content_hash,
            source_machine_id,
            doc.provenance,
            doc.published_at,
        )

    def _insert_chunks(
        self, cur, doc: SourceDocument, vectors: Sequence[Sequence[float]], embedder: Embedder
    ) -> None:
        """Set-based insert of a document's chunks + embeddings.

        ``chunk_seq_id`` is IDENTITY (server-assigned); the embedding is marshaled
        with the confirmed single ``CAST(? AS VECTOR(n))`` (param =
        ``json.dumps(floats)``).
        """
        sql = (
            "INSERT INTO memory.chunks "
            "(chunk_id, document_id, seq, chunk_text, meta, "
            "embedding_model, embedding_dim, embedding) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, CAST(? AS VECTOR({self.embedding_dim})))"
        )
        rows = []
        for chunk, vector in zip(doc.chunks, vectors):
            rows.append(
                (
                    f"mem:{doc.doc_id}:{chunk.seq}",
                    doc.doc_id,
                    chunk.seq,
                    chunk.text,
                    json.dumps(chunk.meta),
                    embedder.model_name,
                    embedder.dim,
                    json.dumps([float(x) for x in vector]),
                )
            )
        if rows:
            cur.executemany(sql, rows)

    def _update_document_metadata(
        self, cur, doc: SourceDocument, source_machine_id: Optional[str]
    ) -> None:
        cur.execute(
            "UPDATE memory.documents SET title = ?, source_type = ?, source_path = ?, "
            "source_machine_id = ?, provenance = ?, published_at = ?, "
            "updated_at = SYSDATETIMEOFFSET() WHERE document_id = ?",
            doc.title,
            doc.source_type,
            doc.source_path,
            source_machine_id,
            doc.provenance,
            doc.published_at,
            doc.doc_id,
        )

    # -- ingest ------------------------------------------------------------ #
    def ingest_documents(
        self,
        source_docs: Sequence[SourceDocument],
        embedder: Embedder,
        *,
        source_machine_id: Optional[str] = None,
    ) -> dict:
        """Persist SourceDocuments to SQL Server: dedup, replace, update metadata.

        Mirrors the local store's ingest exactly (classify → batch-embed →
        per-document write) and returns the same summary shape. Each document is
        its own tiny transaction (``conn.commit()`` on success / ``conn.rollback()``
        on failure, the same per-item posture as
        :meth:`SqlStore._materialize_sessions`), so a per-document fault (recorded
        in ``documents_failed``) or a content-hash race (reconciled to the existing
        row) is isolated without leaving a half-written document or poisoning the
        rest of the batch.
        """
        if embedder.dim != self.embedding_dim:
            raise ValueError(
                f"embedder dim {embedder.dim} != store dim {self.embedding_dim}; "
                "construct the store with embedding_dim=embedder.dim"
            )
        summary = _new_summary()
        docs = list(source_docs)

        # 1) Scoped read of existing document metadata for dedup / metadata compare.
        conn = self._connect()
        try:
            cur = conn.cursor()
            existing, hash_owner = self._preload(
                cur, [d.doc_id for d in docs], [d.content_hash for d in docs]
            )
        finally:
            conn.close()

        # 2) Classify (no embedder yet).
        plan = _classify(docs, existing, hash_owner, source_machine_id)
        summary["documents_unchanged"] += plan.unchanged + plan.reconciled
        summary["documents_skipped_empty"] += plan.skipped_empty

        # 3) Batch-embed every chunk of every (re)written document in one call.
        vectors_by_doc = _embed_documents(plan.to_write, embedder)

        # 4) Write on one connection; each document is its OWN tiny transaction
        #    (commit on success / rollback on failure — the same per-item posture
        #    as SqlStore._materialize_sessions), so a fault or a content-hash race
        #    is isolated and never leaves a half-written document behind.
        integrity_error = self._integrity_error()
        conn = self._connect()
        try:
            cur = conn.cursor()
            for doc in plan.to_write:
                try:
                    existed = self._delete_document(cur, doc.doc_id)
                    self._insert_document(cur, doc, source_machine_id)
                    self._insert_chunks(
                        cur, doc, vectors_by_doc[doc.doc_id], embedder
                    )
                    conn.commit()
                except integrity_error:
                    # content_hash UNIQUE race: a concurrent writer inserted this
                    # content under a different id between our preload and now. Roll
                    # the partial write back and reconcile to the existing row (the
                    # same integrity-race posture as SqlStore.upsert_repository).
                    conn.rollback()
                    summary["documents_unchanged"] += 1
                    continue
                except Exception as exc:  # per-document fault tolerance
                    conn.rollback()
                    summary["documents_failed"].append(
                        {"doc_id": doc.doc_id, "source_path": doc.source_path, "error": str(exc)}
                    )
                    continue
                summary["documents_replaced" if existed else "documents_new"] += 1
                summary["chunks_written"] += len(doc.chunks)
                _bucket(summary, doc)

            for doc in plan.to_update:
                try:
                    self._update_document_metadata(cur, doc, source_machine_id)
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    summary["documents_failed"].append(
                        {"doc_id": doc.doc_id, "source_path": doc.source_path, "error": str(exc)}
                    )
                    continue
                summary["documents_metadata_updated"] += 1

            for doc in plan.to_remove:
                try:
                    self._delete_document(cur, doc.doc_id)
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    summary["documents_failed"].append(
                        {"doc_id": doc.doc_id, "source_path": doc.source_path, "error": str(exc)}
                    )
                    continue
                summary["documents_removed"] += 1
        finally:
            conn.close()
        return summary

    # -- status ------------------------------------------------------------ #
    def stats(self) -> dict:
        """Corpus statistics: document/chunk counts by source_type + model info.

        Feeds ``/v1/memory/status`` (Slice 2); mirrors the local store's stats
        shape.
        """
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT source_type, COUNT(*) AS n FROM memory.documents "
                "GROUP BY source_type"
            )
            doc_rows = self._rows(cur)
            cur.execute(
                "SELECT d.source_type AS source_type, COUNT(*) AS n "
                "FROM memory.chunks c JOIN memory.documents d "
                "ON d.document_id = c.document_id GROUP BY d.source_type"
            )
            chunk_rows = self._rows(cur)
            cur.execute("SELECT COUNT(*) AS n FROM memory.documents")
            total_docs = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) AS n FROM memory.chunks")
            total_chunks = cur.fetchone()[0]
            cur.execute(
                "SELECT DISTINCT embedding_model FROM memory.chunks "
                "WHERE embedding_model IS NOT NULL"
            )
            models = [r[0] for r in cur.fetchall()]
            cur.execute(
                "SELECT DISTINCT embedding_dim FROM memory.chunks "
                "WHERE embedding_dim IS NOT NULL"
            )
            dims = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()

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


# --------------------------------------------------------------------------- #
# In-memory store (tests) — dict-backed parity double, NO SQL
# --------------------------------------------------------------------------- #
class FakeMemoryStore:
    """In-memory :class:`MemoryStore` for the endpoint tests (no database, no SQL).

    Mirrors :class:`MemoryStore`'s method surface and semantics via the SAME
    shared classify/embed/summary primitives, so ingest behavior (3-way classify,
    content-hash dedup + cross-identity reconcile, replace-on-change,
    provenance) is proven against a pure fake. Vectors are stored in a dict — the
    card's load-bearing point: a MemoryStore's natural double must fake vector
    storage, which no in-memory SQL fake can do (Slice 2's cosine read path will
    consume these dicts).
    """

    def __init__(self, embedding_dim: int = DEFAULT_EMBEDDING_DIM):
        self.embedding_dim = int(embedding_dim)
        self.documents: Dict[str, Dict[str, Any]] = {}  # document_id -> row
        self.chunks: Dict[str, Dict[str, Any]] = {}  # chunk_id -> row
        self.vectors: Dict[str, List[float]] = {}  # chunk_id -> embedding
        self._hash_owner: Dict[str, str] = {}  # content_hash -> document_id

    def _delete_document(self, document_id: str) -> bool:
        existed = document_id in self.documents
        row = self.documents.pop(document_id, None)
        if row is not None:
            ch = row.get("content_hash")
            if ch and self._hash_owner.get(ch) == document_id:
                self._hash_owner.pop(ch, None)
        for cid in [c for c, r in self.chunks.items() if r["document_id"] == document_id]:
            self.chunks.pop(cid, None)
            self.vectors.pop(cid, None)
        return existed

    def _write_document(
        self,
        doc: SourceDocument,
        vectors: Sequence[Sequence[float]],
        embedder: Embedder,
        source_machine_id: Optional[str],
    ) -> None:
        self.documents[doc.doc_id] = {
            "document_id": doc.doc_id,
            "title": doc.title,
            "source_type": doc.source_type,
            "source_path": doc.source_path,
            "content_hash": doc.content_hash,
            "source_machine_id": source_machine_id,
            "provenance": doc.provenance,
            "published_at": doc.published_at,
        }
        if doc.content_hash:
            self._hash_owner[doc.content_hash] = doc.doc_id
        for chunk, vector in zip(doc.chunks, vectors):
            cid = f"mem:{doc.doc_id}:{chunk.seq}"
            self.chunks[cid] = {
                "chunk_id": cid,
                "document_id": doc.doc_id,
                "seq": chunk.seq,
                "chunk_text": chunk.text,
                "meta": dict(chunk.meta),
                "embedding_model": embedder.model_name,
                "embedding_dim": embedder.dim,
            }
            self.vectors[cid] = [float(x) for x in vector]

    def _update_document_metadata(
        self, doc: SourceDocument, source_machine_id: Optional[str]
    ) -> None:
        row = self.documents.get(doc.doc_id)
        if row is None:  # pragma: no cover - to_update implies the row exists
            return
        row.update(
            {
                "title": doc.title,
                "source_type": doc.source_type,
                "source_path": doc.source_path,
                "source_machine_id": source_machine_id,
                "provenance": doc.provenance,
                "published_at": doc.published_at,
            }
        )

    def ingest_documents(
        self,
        source_docs: Sequence[SourceDocument],
        embedder: Embedder,
        *,
        source_machine_id: Optional[str] = None,
    ) -> dict:
        if embedder.dim != self.embedding_dim:
            raise ValueError(
                f"embedder dim {embedder.dim} != store dim {self.embedding_dim}; "
                "construct the store with embedding_dim=embedder.dim"
            )
        summary = _new_summary()
        docs = list(source_docs)
        existing = {did: dict(row) for did, row in self.documents.items()}
        hash_owner = dict(self._hash_owner)
        plan = _classify(docs, existing, hash_owner, source_machine_id)
        summary["documents_unchanged"] += plan.unchanged + plan.reconciled
        summary["documents_skipped_empty"] += plan.skipped_empty

        vectors_by_doc = _embed_documents(plan.to_write, embedder)
        for doc in plan.to_write:
            existed = self._delete_document(doc.doc_id)
            self._write_document(
                doc, vectors_by_doc[doc.doc_id], embedder, source_machine_id
            )
            summary["documents_replaced" if existed else "documents_new"] += 1
            summary["chunks_written"] += len(doc.chunks)
            _bucket(summary, doc)
        for doc in plan.to_update:
            self._update_document_metadata(doc, source_machine_id)
            summary["documents_metadata_updated"] += 1
        for doc in plan.to_remove:
            self._delete_document(doc.doc_id)
            summary["documents_removed"] += 1
        return summary

    def stats(self) -> dict:
        by_source_type: Dict[str, dict] = {}
        for row in self.documents.values():
            by_source_type.setdefault(
                row["source_type"], {"documents": 0, "chunks": 0}
            )["documents"] += 1
        for c in self.chunks.values():
            st = self.documents[c["document_id"]]["source_type"]
            by_source_type.setdefault(st, {"documents": 0, "chunks": 0})["chunks"] += 1
        models = sorted(
            {c["embedding_model"] for c in self.chunks.values() if c.get("embedding_model")}
        )
        dims = sorted(
            {c["embedding_dim"] for c in self.chunks.values() if c.get("embedding_dim") is not None}
        )
        return {
            "total_documents": len(self.documents),
            "total_chunks": len(self.chunks),
            "by_source_type": by_source_type,
            "embedding_models": models,
            "embedding_dims": dims,
        }


__all__ = ["MemoryStore", "FakeMemoryStore"]
