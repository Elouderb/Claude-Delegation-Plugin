#!/usr/bin/env python3
"""One-time ETL: migrate the local RAG corpus into central SQL Server ``memory.*``.

Card 2f1fe295 (epic 6fead93b, Slice 1 completion). Copies every document/chunk
out of the machine-global local corpus (``~/.agent-os/central-memory/memory.sqlite``
— sqlite + sqlite-vec, see ``mcp/memory/store.py``) into the central
``memory.documents`` / ``memory.chunks`` tables (migration 008, already applied)
on the live SQL Server, REUSING the existing 384-dim ``BAAI/bge-small-en-v1.5``
embeddings verbatim. This script never embeds anything — every vector it writes
came from the local ``chunks_vec`` table.

Identity is preserved exactly: the local ``documents.doc_id`` becomes
``memory.documents.document_id`` verbatim, and the local chunk_id scheme
(``mem:<doc_id>:<seq>``) is reproduced byte-for-byte by reusing
:meth:`services.api.memory_store.MemoryStore._insert_chunks` (the same helper
the live hub-ingest write path uses) — this script supplies the vectors *and*
per-document ``model_name``/``dim`` values, so nothing in that helper's SQL
shape or column marshaling can drift from the natively-ingested path.

Run (direct script invocation, like ``scripts/manage_keys.py`` — NOT
``python -m``, so this file's own sys.path bootstrap below is what makes the
top-level ``services``/``memory_core``/``contracts``/``database`` packages
resolve regardless of the caller's working directory)::

    python scripts/memory_etl_sql_server.py --dry-run
    python scripts/memory_etl_sql_server.py

``--source`` overrides the input sqlite (default: the machine-global
``~/.agent-os/central-memory/memory.sqlite``, honoring the
``AGENT_OS_MEMORY_HOME`` override the local subsystem itself uses).

Design
------
1. **Extract** (:func:`extract`). The source sqlite is NEVER opened directly
   for reading/writing — it is snapshotted via SQLite's Online Backup API
   (``sqlite3.Connection.backup()``) into a fresh empty sqlite file in a
   private ``tempfile.TemporaryDirectory`` first, and only that COPY is read.
   A plain multi-file ``shutil.copy2`` of the main db + ``-wal``/``-shm``
   sidecars was considered and REJECTED: three separate file copies are not
   atomic, so a concurrent writer's checkpoint completing between copying the
   main file and the ``-wal`` can silently lose the most-recently-committed
   document(s) with no error — unacceptable for a one-time, lossless corpus
   migration. ``backup()`` is race-free by construction (a single guarded
   page-copy operation via SQLite's own C API), correctly captures
   committed-but-not-yet-checkpointed WAL content (a WAL-mode reader always
   sees the latest committed state transparently, sidecars included), and
   copies the ``chunks_vec`` vec0 shadow tables (the embeddings) byte-for-byte
   intact. The source connection is opened READ-ONLY (URI ``mode=ro``) —
   stricter than a file copy, this call cannot write a single byte to the
   original — and does NOT load the sqlite-vec extension (``backup()`` is a
   raw page copy; it never needs to understand the vec0 virtual table module
   to copy its shadow tables). A concurrent MCP ``memory_query``/
   ``memory_ingest`` therefore can never be blocked or corrupted by this
   script, and the script can never accidentally mutate the real corpus (it
   only ever opens the copy, in the tempdir, and the tempdir is deleted when
   the ``with`` block exits).

   Vectors are read from ``chunks_vec`` (a sqlite-vec ``vec0`` virtual table,
   so the read connection loads the extension exactly like the local store
   does) via a batched ``WHERE chunk_rowid IN (...)`` — the same query SHAPE
   ``mcp/memory/store.py.MemoryStore._delete_document`` already uses against
   this table (a proven-supported vec0 access pattern), keyed on
   ``chunks_vec.chunk_rowid == chunks.id`` (verified in
   ``mcp/memory/store.py._insert_chunks``: it writes
   ``(id, sqlite_vec.serialize_float32(vector))`` pairs into
   ``chunks_vec(chunk_rowid, embedding)``). The inverse of that write is
   ``struct.unpack(f"<{dim}f", blob)`` — sqlite-vec's ``serialize_float32``
   packs a plain little-endian float32 array with no header.

2. **Load** (:func:`_load`, invoked by :func:`run`). One connection for the
   whole run (via ``database.migrate.connect``, autocommit off), one
   ``commit()``/``rollback()`` per document — mirrors the per-document-fault-
   tolerance posture of ``services.api.memory_store.MemoryStore.ingest_documents``,
   just without its embed step (there is nothing to embed).

   * ``memory.chunks`` rows are written via
     :meth:`services.api.memory_store.MemoryStore._insert_chunks` UNCHANGED —
     it already accepts pre-computed vectors (it only ever *reads*
     ``embedder.model_name``/``embedder.dim`` for the two per-chunk metadata
     columns; it never calls ``embed_documents``/``embed_query``), so this
     script supplies a tiny :class:`_PrecomputedEmbedder` stub carrying the
     LOCAL row's own ``embedding_model``/``embedding_dim`` and reuses the
     helper verbatim for exact column/marshaling parity with the native
     hub-ingest path (including the confirmed single
     ``CAST(? AS VECTOR(384))`` shape).
   * ``memory.documents`` rows use a THIN ETL-specific insert
     (:func:`_insert_document_etl`) rather than
     :meth:`~services.api.memory_store.MemoryStore._insert_document` verbatim,
     for exactly one reason: the reused helper always defaults
     ``created_at``/``updated_at`` to ``SYSDATETIMEOFFSET()`` (the "now" of a
     native ingest), but the card requires preserving the LOCAL row's
     ``ingested_at`` as both columns (an ETL'd row's history predates this
     migration). ``_insert_document_etl`` mirrors the reused helper's exact
     column list/order and adds ``created_at``/``updated_at`` via
     ``COALESCE(TRY_CONVERT(DATETIMEOFFSET, ?), SYSDATETIMEOFFSET())`` so a
     missing/malformed local timestamp falls back to the same default the
     reused helper would have used, rather than failing the row. Every other
     column (including forcing ``provenance='etl'``, regardless of the local
     row's own provenance) matches the reused helper exactly.

3. **Idempotency**. :meth:`~services.api.memory_store.MemoryStore._preload`
   (the SAME scoped by-id/by-hash preload the live ingest path uses) is reused
   to build the existing ``document_id`` set and ``content_hash -> document_id``
   map; a source document already present by id OR by content hash is skipped
   (``documents_skipped``), and an in-batch ``content_hash`` collision within
   the SAME run is caught the same way (so a re-run after a partial failure,
   or a run against a partially-populated hub, is always safe).

4. **--dry-run**. Extraction + a dimension/count report, PLUS a read-only
   ``_preload`` call against SQL Server to compute an accurate
   "would_skip_existing" / "would_insert" preview (a real, connected preview,
   not a guess) — but the function returns before any INSERT/UPDATE/DELETE is
   ever issued, so it is safe to run repeatedly.

Corpus privacy
--------------
This script is GENERIC: it contains no corpus data, titles, or content. Do not
add example ids/titles/content pulled from a real run into this file, its
tests, or its docstrings — the corpus is private, this repo is public
(``scripts/memory_eval.py`` is the sibling precedent for what NOT to do here;
that file is gitignored specifically because it embeds corpus-derived query
examples — this one is committed and must stay corpus-free).
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Bootstrap the repo root onto sys.path so the top-level packages resolve
# regardless of the caller's working directory (mirrors scripts/manage_keys.py
# / mcp/task_inbox.py's direct-script-invocation convention).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is a core dep, but stay importable
    pass

from contracts import ensure_machine_identity  # noqa: E402
from database import migrate as db_migrate  # noqa: E402
from memory_core.adapters import ChunkInput, SourceDocument, VALID_SOURCE_TYPES  # noqa: E402
from memory_core.availability import (  # noqa: E402
    DEFAULT_EMBEDDING_DIM,
    default_db_path,
    resolve_sqlite_module,
)
from services.api.memory_store import MemoryStore  # noqa: E402

_PROVENANCE = "etl"

# Batch size for chunked WHERE...IN queries against chunks_vec (mirrors
# mcp/memory/store.py._load_candidate_rows / services/api/memory_store.py._IN_CHUNK).
_IN_CHUNK = 500


# --------------------------------------------------------------------------- #
# Extraction data model (deliberately independent of memory_core.adapters'
# SourceDocument/ChunkInput until load time, so extraction never has to fake
# fields those classes don't need — e.g. the local ingested_at timestamp).
# --------------------------------------------------------------------------- #
@dataclass
class ExtractedChunk:
    chunk_id: str
    doc_id: str
    seq: int
    text: str
    meta: dict
    embedding_model: Optional[str]
    # Always a concrete int by the time extract() builds one (falls back to
    # DEFAULT_EMBEDDING_DIM when the local row's own column is NULL/0) — never
    # Optional, so downstream dim-consistency checks need no None-narrowing.
    embedding_dim: int
    vector: Optional[List[float]]


@dataclass
class ExtractedDocument:
    doc_id: str
    title: Optional[str]
    source_type: str
    source_path: Optional[str]
    content_hash: Optional[str]
    provenance: Optional[str]
    published_at: Optional[str]
    ingested_at: Optional[str]
    chunks: List[ExtractedChunk] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Extraction: race-free backup -> read-only-in-spirit connection -> rows
# --------------------------------------------------------------------------- #
def _backup_source_to_copy(source_path: Path, dest_dir: Path) -> Path:
    """Race-free snapshot of ``source_path`` into ``dest_dir`` via SQLite's
    Online Backup API. Returns the path of the fresh copy.

    A plain ``shutil.copy2`` of the main db file plus its ``-wal``/``-shm``
    sidecars is NOT atomic across the three separate file copies: if a
    concurrent writer's checkpoint completes between copying the main file and
    the ``-wal``, the copy can silently lose the most-recently-committed
    document(s) with no error — unacceptable for a one-time, lossless corpus
    migration. ``Connection.backup()`` copies pages under SQLite's own
    locking, so it is race-free by construction, correctly captures
    committed-but-not-yet-checkpointed WAL content (a WAL-mode reader always
    sees the latest committed state transparently), and copies the
    ``chunks_vec`` vec0 shadow tables (the embeddings) intact.

    The source is opened READ-ONLY (URI ``mode=ro``) — this call never writes
    a single byte to the original file, stricter than a plain file copy. The
    sqlite-vec extension is NOT loaded on the source connection: ``backup()``
    is a raw page copy, so it never needs to understand the vec0 virtual table
    module to copy its shadow tables byte-for-byte. Both connections use the
    SAME resolved sqlite module (pysqlite3 or stdlib) — ``backup()`` requires
    its target to be an instance of the SAME binding's ``Connection`` class,
    so mixing a stdlib ``sqlite3`` source with a ``pysqlite3`` destination (or
    vice versa) would raise a ``TypeError``.
    """
    sqlite_module, _ = resolve_sqlite_module()
    dest_path = dest_dir / source_path.name
    source_conn = sqlite_module.connect(f"file:{source_path}?mode=ro", uri=True)
    try:
        dest_conn = sqlite_module.connect(str(dest_path))
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()
    return dest_path


def _import_sqlite_vec():
    import sqlite_vec  # local import: only needed when actually extracting

    return sqlite_vec


def _connect_local_copy(db_path: Path):
    """Open the (already-copied) local sqlite with the sqlite-vec extension
    loaded, exactly like ``mcp/memory/store.py.MemoryStore._connect`` — needed
    to query the ``chunks_vec`` vec0 virtual table at all. Opened read-write
    (not immutable) so SQLite can replay any copied ``-wal`` sidecar; safe
    because this path is always a private tempdir copy, never the original.
    """
    sqlite_module, _ = resolve_sqlite_module()
    sqlite_vec = _import_sqlite_vec()
    conn = sqlite_module.connect(str(db_path))
    conn.row_factory = sqlite_module.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _load_meta(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _unpack_vector(blob: bytes, dim: int) -> Optional[List[float]]:
    """Inverse of ``sqlite_vec.serialize_float32``: a packed little-endian
    float32 array with no header. Returns None (rather than raising) on a
    length mismatch, so one corrupt row degrades gracefully instead of
    aborting the whole extraction; the caller counts and reports it."""
    try:
        return list(struct.unpack(f"<{dim}f", blob))
    except struct.error:
        return None


def _fetch_vectors(conn, chunk_rowids: Sequence[int]) -> Dict[int, bytes]:
    """chunk_rowid -> raw embedding blob, batched WHERE...IN.

    Mirrors the query SHAPE ``mcp/memory/store.py.MemoryStore._delete_document``
    already uses against this exact table
    (``DELETE FROM chunks_vec WHERE chunk_rowid IN (...)``) — a proven-
    supported vec0 access pattern for this codebase, here as a SELECT.
    """
    out: Dict[int, bytes] = {}
    ids = list(chunk_rowids)
    for i in range(0, len(ids), _IN_CHUNK):
        batch = ids[i : i + _IN_CHUNK]
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT chunk_rowid, embedding FROM chunks_vec WHERE chunk_rowid IN ({placeholders})",
            batch,
        ).fetchall()
        for r in rows:
            out[r["chunk_rowid"]] = r["embedding"]
    return out


def extract(source_path: Path) -> Tuple[List[ExtractedDocument], Dict[str, Any]]:
    """Backup ``source_path`` to a private tempdir (race-free, see
    :func:`_backup_source_to_copy`) and extract every document, its chunks,
    and their vectors from the COPY. Returns ``(documents, stats)``.

    ``stats`` reports raw extraction counts (documents, chunks, chunks with a
    missing or corrupt vector) independent of any later dimension/dedup
    reporting (:func:`_dim_report`) or load-time validation (:func:`_load`).
    """
    source_path = Path(source_path).expanduser()
    if not source_path.exists():
        raise FileNotFoundError(f"local corpus sqlite not found: {source_path}")

    with tempfile.TemporaryDirectory(prefix="agent_os_memory_etl_") as tmp:
        copy_path = _backup_source_to_copy(source_path, Path(tmp))
        conn = _connect_local_copy(copy_path)
        try:
            doc_rows = conn.execute(
                "SELECT doc_id, title, source_type, source_path, content_hash, "
                "provenance, published_at, ingested_at FROM documents ORDER BY doc_id"
            ).fetchall()
            chunk_rows = conn.execute(
                "SELECT id, chunk_id, doc_id, seq, text, meta, embedding_model, "
                "embedding_dim FROM chunks ORDER BY doc_id, seq"
            ).fetchall()
            vec_blobs = _fetch_vectors(conn, [r["id"] for r in chunk_rows])
        finally:
            conn.close()

    chunks_by_doc: Dict[str, List[ExtractedChunk]] = {}
    missing_vectors = 0
    corrupt_vectors = 0
    for row in chunk_rows:
        dim = row["embedding_dim"] or DEFAULT_EMBEDDING_DIM
        blob = vec_blobs.get(row["id"])
        if blob is None:
            missing_vectors += 1
            vector: Optional[List[float]] = None
        else:
            vector = _unpack_vector(blob, dim)
            if vector is None:
                corrupt_vectors += 1
        chunk = ExtractedChunk(
            chunk_id=row["chunk_id"],
            doc_id=row["doc_id"],
            seq=row["seq"],
            text=row["text"],
            meta=_load_meta(row["meta"]),
            embedding_model=row["embedding_model"],
            embedding_dim=dim,
            vector=vector,
        )
        chunks_by_doc.setdefault(row["doc_id"], []).append(chunk)

    documents: List[ExtractedDocument] = []
    for row in doc_rows:
        documents.append(
            ExtractedDocument(
                doc_id=row["doc_id"],
                title=row["title"],
                source_type=row["source_type"],
                source_path=row["source_path"],
                content_hash=row["content_hash"],
                provenance=row["provenance"],
                published_at=row["published_at"],
                ingested_at=row["ingested_at"],
                chunks=chunks_by_doc.get(row["doc_id"], []),
            )
        )

    stats = {
        "documents": len(documents),
        "chunks": len(chunk_rows),
        "missing_vectors": missing_vectors,
        "corrupt_vectors": corrupt_vectors,
    }
    return documents, stats


# --------------------------------------------------------------------------- #
# --dry-run reporting (dimension sanity, per-doc chunk stats, samples)
# --------------------------------------------------------------------------- #
def _dim_report(documents: List[ExtractedDocument], expected_dim: int) -> Dict[str, Any]:
    dims_seen = set()
    mismatched = 0
    usable_chunks = 0
    per_doc_counts: List[int] = []
    for doc in documents:
        per_doc_counts.append(len(doc.chunks))
        for c in doc.chunks:
            if c.vector is None:
                continue
            usable_chunks += 1
            if c.embedding_dim is not None:
                dims_seen.add(c.embedding_dim)
            if len(c.vector) != expected_dim or c.embedding_dim != expected_dim:
                mismatched += 1
    sample = [
        {"doc_id": d.doc_id, "title": d.title, "chunk_count": len(d.chunks)}
        for d in documents[:5]
    ]
    return {
        "documents": len(documents),
        "usable_chunks": usable_chunks,
        "expected_dim": expected_dim,
        "embedding_dims_seen": sorted(dims_seen),
        "chunks_with_dim_mismatch": mismatched,
        "chunk_count_min": min(per_doc_counts) if per_doc_counts else 0,
        "chunk_count_max": max(per_doc_counts) if per_doc_counts else 0,
        "chunk_count_mean": (
            round(sum(per_doc_counts) / len(per_doc_counts), 2) if per_doc_counts else 0
        ),
        "sample_documents": sample,
    }


# --------------------------------------------------------------------------- #
# Load: reuse MemoryStore._preload (dedup) + MemoryStore._insert_chunks
# (vectors), a thin ETL-specific document insert (created_at/updated_at).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _PrecomputedEmbedder:
    """Duck-types ``memory_core.embeddings.Embedder`` for ``_insert_chunks``.

    ``_insert_chunks`` never calls ``embed_documents``/``embed_query`` — every
    vector is supplied directly from the local corpus — so those two methods
    exist only to satisfy the structural Embedder protocol and raise if ever
    (incorrectly) invoked.
    """

    model_name: str
    dim: int

    def embed_documents(self, texts: List[str]) -> List[List[float]]:  # pragma: no cover
        raise NotImplementedError("ETL supplies precomputed vectors; it never re-embeds")

    def embed_query(self, text: str) -> List[float]:  # pragma: no cover
        raise NotImplementedError("ETL supplies precomputed vectors; it never re-embeds")


def _validate_document(doc: ExtractedDocument, expected_dim: int) -> Optional[str]:
    """Return a short, corpus-safe failure reason if ``doc`` cannot be loaded,
    else ``None``. SHARED between the real load path (:func:`_load`) and the
    ``--dry-run`` preview (:func:`run`) so the two can never drift on what
    counts as a failure, and between the pre-checks that used to live inline
    in :func:`_load` (embedding-model/dim consistency, chunk_id shape) plus
    the embedding-dim-vs-store-width check — all now caught HERE, before any
    INSERT is attempted, instead of failing late via a raw driver exception.

    Every reason string is fully generic (no interpolated row/content values)
    — the ``doc_id`` a caller already carries alongside this reason is enough
    to look up the offending row directly; see the module's Corpus privacy
    note and the ``documents_failed`` sanitization in :func:`_sanitize_exception`.
    """
    if not doc.content_hash:
        return "missing content_hash"
    if doc.source_type not in VALID_SOURCE_TYPES:
        return "invalid source_type"
    usable_chunks = [c for c in doc.chunks if c.vector is not None]
    if not usable_chunks:
        return "no chunks with a usable vector"
    model_names = {c.embedding_model for c in usable_chunks}
    dims = {c.embedding_dim for c in usable_chunks}
    if len(model_names) != 1 or len(dims) != 1:
        return "inconsistent embedding_model/embedding_dim across chunks"
    (dim,) = dims
    if dim != expected_dim:
        return "embedding_dim does not match the configured store dimension"
    expected_ids = [f"mem:{doc.doc_id}:{c.seq}" for c in usable_chunks]
    if any(c.chunk_id != e for c, e in zip(usable_chunks, expected_ids)):
        return "chunk_id does not match mem:<document_id>:<seq>"
    return None


def _sanitize_exception(exc: Exception) -> str:
    """A short, corpus-safe failure reason for an INSERT-time exception.

    Raw driver exception text can echo bound parameter values (e.g. a SQL
    Server truncation/constraint error sometimes quotes the offending string),
    which could leak corpus content into a report a user pastes into a chat or
    ticket. Reports only the exception's type and, for a pyodbc-style error
    whose first arg is a short SQLSTATE code, that code — never the full
    driver message.
    """
    exc_type = type(exc).__name__
    args = getattr(exc, "args", None)
    sqlstate = args[0] if args and isinstance(args[0], str) and len(args[0]) <= 8 else None
    return f"{exc_type} ({sqlstate})" if sqlstate else exc_type


def _insert_document_etl(
    cur, doc: ExtractedDocument, source_machine_id: Optional[str], provenance: str
) -> None:
    """Direct INSERT for ``memory.documents``.

    Mirrors ``services.api.memory_store.MemoryStore._insert_document``'s
    column list/order exactly, with the two additions that helper cannot
    express: ``created_at``/``updated_at`` are seeded from the LOCAL row's
    ``ingested_at`` (preserving the original ingest time) via
    ``COALESCE(TRY_CONVERT(DATETIMEOFFSET, ?), SYSDATETIMEOFFSET())`` so a
    missing/malformed value falls back to the same default the reused helper
    would have used, rather than failing the row. ``provenance`` is passed
    explicitly (``'etl'``) rather than the source row's own value.
    """
    cur.execute(
        "INSERT INTO memory.documents "
        "(document_id, title, source_type, source_path, content_hash, "
        "source_machine_id, provenance, published_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
        "COALESCE(TRY_CONVERT(DATETIMEOFFSET, ?), SYSDATETIMEOFFSET()), "
        "COALESCE(TRY_CONVERT(DATETIMEOFFSET, ?), SYSDATETIMEOFFSET()))",
        doc.doc_id,
        doc.title,
        doc.source_type,
        doc.source_path,
        doc.content_hash,
        source_machine_id,
        provenance,
        doc.published_at,
        doc.ingested_at,
        doc.ingested_at,
    )


def _new_load_summary() -> Dict[str, Any]:
    return {
        "documents_inserted": 0,
        "documents_skipped": 0,
        "documents_failed": [],
        "chunks_inserted": 0,
    }


def _load(
    store: MemoryStore,
    conn,
    cur,
    documents: List[ExtractedDocument],
    existing: Dict[str, dict],
    hash_owner: Dict[str, str],
    source_machine_id: Optional[str],
) -> Dict[str, Any]:
    """Write every not-yet-present document to SQL Server, one commit each.

    ``existing``/``hash_owner`` come from :meth:`MemoryStore._preload` (the
    SAME scoped by-id/by-hash preload the live hub-ingest path uses).
    ``seen_hashes`` extends that map as documents are written so an in-batch
    content-hash collision (two local doc_ids sharing one hash) is caught the
    same way a cross-request collision is on the live path.
    """
    summary = _new_load_summary()
    seen_hashes: Dict[str, str] = dict(hash_owner)

    for doc in documents:
        if doc.doc_id in existing:
            summary["documents_skipped"] += 1
            continue
        if doc.content_hash and doc.content_hash in seen_hashes:
            summary["documents_skipped"] += 1
            continue

        reason = _validate_document(doc, store.embedding_dim)
        if reason is not None:
            summary["documents_failed"].append({"doc_id": doc.doc_id, "error": reason})
            continue

        # _validate_document already confirmed: >=1 usable chunk, a single
        # consistent embedding_model/embedding_dim across them (matching the
        # store's configured width), a non-empty content_hash, and every
        # chunk_id in mem:<doc>:<seq> shape — so building the
        # embedder/SourceDocument here is safe.
        assert doc.content_hash  # narrows Optional[str] -> str for the type checker
        usable_chunks = [c for c in doc.chunks if c.vector is not None]
        embedder = _PrecomputedEmbedder(
            model_name=str(usable_chunks[0].embedding_model), dim=usable_chunks[0].embedding_dim
        )
        source_doc = SourceDocument(
            doc_id=doc.doc_id,
            title=doc.title or "",
            source_type=doc.source_type,
            source_path=doc.source_path,
            content_hash=doc.content_hash,
            provenance=_PROVENANCE,
            published_at=doc.published_at,
            chunks=[ChunkInput(seq=c.seq, text=c.text, meta=c.meta) for c in usable_chunks],
        )
        # usable_chunks was filtered on `c.vector is not None` above, but that
        # narrowing doesn't survive into this separate comprehension for the
        # type checker, so filter again inline (a no-op at runtime) to get a
        # genuine List[List[float]] rather than a cast.
        vectors: List[List[float]] = [c.vector for c in usable_chunks if c.vector is not None]

        try:
            _insert_document_etl(cur, doc, source_machine_id, _PROVENANCE)
            store._insert_chunks(cur, source_doc, vectors, embedder)  # noqa: SLF001 - see module docstring
            conn.commit()
        except Exception as exc:  # per-document fault tolerance, mirrors MemoryStore.ingest_documents
            conn.rollback()
            summary["documents_failed"].append(
                {"doc_id": doc.doc_id, "error": _sanitize_exception(exc)}
            )
            continue

        summary["documents_inserted"] += 1
        summary["chunks_inserted"] += len(usable_chunks)
        if doc.content_hash:
            seen_hashes[doc.content_hash] = doc.doc_id

    return summary


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(
    source: Path,
    *,
    dry_run: bool = False,
    embedding_dim: int = DEFAULT_EMBEDDING_DIM,
    source_machine_id: Optional[str] = None,
    connect=None,
) -> Dict[str, Any]:
    """Extract from ``source`` then either report (--dry-run) or load.

    ``connect`` defaults to :func:`database.migrate.connect`; tests inject a
    fake zero-arg callable so this whole path runs with NO live SQL Server.
    """
    connect = connect or db_migrate.connect
    documents, extract_stats = extract(source)
    store = MemoryStore(connect=connect, embedding_dim=embedding_dim)

    conn = connect()
    try:
        cur = conn.cursor()
        existing, hash_owner = store._preload(  # noqa: SLF001 - see module docstring
            cur,
            [d.doc_id for d in documents],
            [d.content_hash for d in documents if d.content_hash],
        )

        if dry_run:
            report = _dim_report(documents, embedding_dim)
            # Mirrors _load's own per-document skip/validate order exactly (via
            # the SAME _validate_document helper) so this preview can never
            # drift from what a real load would do: would_insert only counts
            # documents that pass both the dedup check AND validation.
            would_skip = 0
            would_fail = 0
            seen_hashes: Dict[str, str] = dict(hash_owner)
            for d in documents:
                if d.doc_id in existing or (d.content_hash and d.content_hash in seen_hashes):
                    would_skip += 1
                    continue
                if _validate_document(d, embedding_dim) is not None:
                    would_fail += 1
                    continue
                if d.content_hash:
                    seen_hashes[d.content_hash] = d.doc_id
            report["would_skip_existing"] = would_skip
            report["would_fail"] = would_fail
            report["would_insert"] = len(documents) - would_skip - would_fail
            return {
                "mode": "dry_run",
                "source": str(source),
                "source_machine_id": source_machine_id,
                "extract": extract_stats,
                "report": report,
            }

        summary = _load(store, conn, cur, documents, existing, hash_owner, source_machine_id)
        summary["mode"] = "load"
        summary["source"] = str(source)
        summary["source_machine_id"] = source_machine_id
        summary["extract"] = extract_stats
        return summary
    finally:
        conn.close()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "One-time ETL: migrate the local RAG corpus into central SQL Server "
            "memory.documents/memory.chunks, reusing existing embeddings (no re-embed)."
        )
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help=(
            "Path to the local corpus sqlite (default: the machine-global "
            "~/.agent-os/central-memory/memory.sqlite, honoring AGENT_OS_MEMORY_HOME)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract + report only (still reads SQL Server read-only for a would-skip preview); no writes.",
    )
    parser.add_argument(
        "--embedding-dim",
        type=int,
        default=DEFAULT_EMBEDDING_DIM,
        help=f"Expected embedding dimension (default: {DEFAULT_EMBEDDING_DIM}).",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    source = args.source or default_db_path()
    identity = ensure_machine_identity()

    if not args.dry_run:
        print(
            f"Loading the local corpus at {source} into central SQL Server "
            f"memory.documents/memory.chunks as machine {identity.machine_id} "
            "(provenance='etl')...",
            file=sys.stderr,
        )

    result = run(
        source=source,
        dry_run=args.dry_run,
        embedding_dim=args.embedding_dim,
        source_machine_id=identity.machine_id,
    )
    print(json.dumps(result, indent=2, default=str))
    return 1 if result.get("documents_failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
