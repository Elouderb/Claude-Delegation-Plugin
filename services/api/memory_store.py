"""Central-memory persistence for the Agent OS API — the hub-side store.

This is the SQL-Server-backed half of central memory (epic 6fead93b): the WRITE
path (``ingest_documents``, Slice 1c) plus the hybrid READ path (``query``, Slice
2) that reproduces the LOCAL sqlite store's retrieval against SQL Server. It
mirrors two established patterns:

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

VECTOR marshaling (card 4d362ffb CORRECTS the original card c8e686a9 claim
below): the embedding is inserted as
``CAST(CAST(? AS NVARCHAR(MAX)) AS VECTOR(384))`` with the bound parameter =
``json.dumps(embedding_floats)``. The original single ``CAST(? AS VECTOR(384))``
was "verified" only against a short, clean synthetic vector (~1.9 KB JSON) that
stays under pyodbc's parameter-streaming threshold; a REALISTIC full-precision
384-dim embedding (~8 KB JSON) is bound as a streamed SQL_WLONGVARCHAR, and SQL
Server 2025 refuses to CAST a streamed parameter directly to VECTOR (SQLSTATE
22018) — this is what failed all 759 documents of the live corpus ETL, and was
reproduced deterministically in sandboxed rollback-only probes. The
NVARCHAR(MAX) hop fixes it; ``json.dumps`` is still the correct param
encoding (a fixed-decimal form was tried and fails).
``chunk_id`` is ``mem:<document_id>:<seq>``, byte-identical to the local scheme.

Applying migration 008 / connecting to the live SQL Server is out of scope here
(lead-gated): the hermetic tests exercise :class:`FakeMemoryStore` + a
deterministic fake embedder and never touch SQL Server.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from memory_core.adapters import VALID_SOURCE_TYPES, SourceDocument, validate_iso_date
from memory_core.availability import DEFAULT_EMBEDDING_DIM
from memory_core.embeddings import Embedder
from memory_core.reranker import Reranker
from memory_core.retrieval import RRF_K, fuse_ranked_ids

logger = logging.getLogger(__name__)

# Cap the IN-list size for the scoped preload so we stay well under SQL Server's
# parameter limit (2100); document batches are tiny in practice.
_IN_CHUNK = 500

# ---- Hybrid-retrieval pool sizing (mirrors mcp/memory/store.py verbatim) ----
# These govern how many vector/lexical candidates to fetch and fuse; reproduced
# here (not imported from the mcp layer) so services.api depends only on
# memory_core, exactly like the shared classify/embed/summary primitives below.
# The local store is the source of truth — keep these in lockstep with it.
_MIN_CANDIDATE_POOL = 50
_CANDIDATE_MULTIPLIER = 5
_VEC_K_LIMIT = 4096
_DEFAULT_RERANK_POOL = 50
_MAX_RERANK_POOL = 1000
_DEFAULT_DEDUP_POOL = 50
_DEDUP_POOL_MULTIPLIER = 10

# Lexical term extraction (mirrors mcp/memory/store.py._fts_query / _WORD_RE).
_WORD_RE = re.compile(r"\w+")
_FTS_MAX_TERMS = 32


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
# Shared hybrid-retrieval primitives (used by BOTH query implementations)
#
# These reproduce the LOCAL store's query semantics (mcp/memory/store.py
# MemoryStore.query L578-707 and its helpers) storage-agnostically, so the SQL
# store and its in-memory fake cannot drift on validation, pool sizing, fusion,
# rerank ordering, per-doc dedup, or the returned row shape. Only candidate
# GENERATION (SQL vs. Python) differs between the two implementations.
# --------------------------------------------------------------------------- #
def _query_terms(text: str) -> List[str]:
    """The capped ``\\w+`` term list for the lexical arm (mirrors _fts_query)."""
    return _WORD_RE.findall(text or "")[:_FTS_MAX_TERMS]


def _contains_query(text: str) -> Optional[str]:
    """Build a SQL Server CONTAINS search condition: quoted terms OR-ed for recall.

    Mirrors mcp/memory/store.py._fts_query. Quoting each ``\\w+`` term neutralizes
    CONTAINS operators/reserved characters in user input (the terms are word
    characters only, so they can never contain a closing quote). Returns None when
    the query has no indexable terms, so the caller skips the lexical arm exactly
    like the local ``fts_ids = []`` path.
    """
    terms = _query_terms(text)
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)


# Okapi BM25 parameters for the hub's lexical re-rank (standard defaults, and the
# exact values validated by the card b81ef155 eval-gate prototype).
_BM25_K1 = 1.5
_BM25_B = 0.75


def _bm25_rerank(terms: Sequence[str], docs: Sequence[tuple]) -> List[str]:
    """Re-order a lexical candidate pool by pool-local Okapi BM25.

    SQL Server's CONTAINSTABLE ``RANK`` is a weaker top-of-list ordering than the
    local FTS5 ``bm25()`` the sqlite store uses, so RRF (which weights rank
    POSITIONS) gets a weaker lexical signal from the hub and the fused nDCG@10
    regresses (card b81ef155 eval gate: -0.086 on keyword-gold). Rescoring the
    SAME CONTAINSTABLE candidate pool with a pool-local BM25 — document
    frequencies computed over the pool, not the whole corpus — restores the
    ordering to net-positive WITHOUT deepening the pool (a deeper pool tested
    worse and slower).

    ``terms`` are the query's ``\\w+`` terms; they are lowercased here (as is the
    document text) so the caller can pass the raw :func:`_query_terms` output.
    ``docs`` is the RANK-ordered ``[(chunk_id, chunk_text), ...]`` pool. Returns
    the chunk_ids reordered by DESCENDING BM25 score; equal scores keep the input
    (RANK) order via the original index, so the result is fully deterministic.

    This is a PURE helper — no DB, no model — and is HUB-ONLY: the local store
    already gets true ``bm25()`` from FTS5 and needs no re-rank. Degenerate inputs
    return the input order unchanged (empty ``terms``, empty ``docs``, or an
    all-empty pool whose average document length is zero); ``docs`` empty yields
    ``[]``.
    """
    if not terms or not docs:
        return [cid for cid, _ in docs]
    query_terms = [t.lower() for t in terms if t]
    if not query_terms:
        return [cid for cid, _ in docs]

    tokenized = [(cid, _WORD_RE.findall((text or "").lower())) for cid, text in docs]
    n = len(tokenized)
    avgdl = sum(len(toks) for _, toks in tokenized) / n
    if avgdl <= 0.0:
        return [cid for cid, _ in docs]

    # Pool-local document frequency per query term.
    df: Dict[str, int] = {}
    for _, toks in tokenized:
        present = set(toks)
        for term in query_terms:
            if term in present:
                df[term] = df.get(term, 0) + 1

    scored: List[tuple] = []
    for idx, (cid, toks) in enumerate(tokenized):
        tf: Dict[str, int] = {}
        for w in toks:
            tf[w] = tf.get(w, 0) + 1
        dl = len(toks)
        score = 0.0
        for term in query_terms:
            f = tf.get(term, 0)
            if not f:
                continue
            d = df.get(term, 0)
            idf = math.log(1 + (n - d + 0.5) / (d + 0.5))
            score += idf * (f * (_BM25_K1 + 1)) / (
                f + _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / avgdl)
            )
        # Negate the score so a plain ascending sort puts the best first; ``idx``
        # is the stable tie-break, preserving the original RANK order for equal
        # scores (and making the sort deterministic without relying on stability).
        scored.append((-score, idx, cid))

    scored.sort()
    return [cid for _, _, cid in scored]


def _candidate_pool(top_k: int) -> int:
    """SQL TOP/kNN candidate pool (mirrors local _candidate_pool)."""
    return min(max(_MIN_CANDIDATE_POOL, top_k * _CANDIDATE_MULTIPLIER), _VEC_K_LIMIT)


def _resolve_dedup_pool(top_k: int) -> int:
    """Fused pool for dedup backfill; SCALES with top_k (mirrors local)."""
    return min(max(_DEFAULT_DEDUP_POOL, top_k * _DEDUP_POOL_MULTIPLIER), _VEC_K_LIMIT)


def _resolve_rerank_pool(rerank_pool: Optional[int], top_k: int) -> int:
    """Validate/normalize the rerank candidate-pool size (mirrors local)."""
    if rerank_pool is None:
        pool = _DEFAULT_RERANK_POOL
    elif isinstance(rerank_pool, bool) or not isinstance(rerank_pool, int) or rerank_pool < 1:
        raise ValueError(f"rerank_pool must be a positive integer, got {rerank_pool!r}")
    elif rerank_pool > _MAX_RERANK_POOL:
        raise ValueError(f"rerank_pool must be <= {_MAX_RERANK_POOL}, got {rerank_pool!r}")
    else:
        pool = rerank_pool
    return max(pool, top_k)


def _validate_query_args(
    embedder: Embedder,
    embedding_dim: int,
    top_k: int,
    date_from: Optional[str],
    date_to: Optional[str],
    source_type: Optional[str],
    max_per_doc: Optional[int],
) -> tuple[Optional[str], Optional[str]]:
    """Mirror the local query's argument validation; returns normalized dates."""
    if embedder.dim != embedding_dim:
        raise ValueError(f"embedder dim {embedder.dim} != store dim {embedding_dim}")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        raise ValueError(f"top_k must be a positive integer, got {top_k!r}")
    date_from = validate_iso_date(date_from, "date_from")
    date_to = validate_iso_date(date_to, "date_to")
    if source_type is not None and source_type not in VALID_SOURCE_TYPES:
        raise ValueError(
            f"invalid source_type {source_type!r}; expected one of {VALID_SOURCE_TYPES}"
        )
    if max_per_doc is not None and (
        isinstance(max_per_doc, bool) or not isinstance(max_per_doc, int) or max_per_doc < 1
    ):
        raise ValueError(
            f"max_per_doc must be a positive integer or None, got {max_per_doc!r}"
        )
    return date_from, date_to


def _resolve_pool_target(
    top_k: int,
    rerank: bool,
    reranker: Optional[Reranker],
    rerank_pool: Optional[int],
    max_per_doc: Optional[int],
) -> int:
    """How many fused candidates to materialize (mirrors local pool_target).

    Exactly top_k without rerank OR dedup (Phase-0 behavior); a rerank pool the
    cross-encoder reorders within; a dedup pool to backfill distinct docs; the max
    of the two when both are on. Never below top_k.
    """
    pool_target = top_k
    if rerank:
        if reranker is None:
            raise ValueError("rerank=True requires a reranker")
        pool_target = max(pool_target, _resolve_rerank_pool(rerank_pool, top_k))
    if max_per_doc is not None:
        pool_target = max(pool_target, _resolve_dedup_pool(top_k))
    return pool_target


def _cosine_distance(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine distance (1 - cosine similarity) — the fake's VECTOR_DISTANCE twin."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - dot / (na * nb)


def _iso_or_none(value: Any) -> Optional[str]:
    """Render a DATETIMEOFFSET (decoded to an aware datetime) as an ISO string.

    Central ``memory.documents`` has no ``ingested_at`` column; ``created_at`` is
    its equivalent and populates the local row shape's ``ingested_at`` field. The
    local store stores that as an ISO string, so we normalize here for parity and
    JSON-serializability.
    """
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _coerce_meta(value: Any) -> dict:
    """Chunk meta as a dict, tolerant of a JSON string (SQL) or a dict (fake)."""
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}


def _result_row(row: dict, score: float) -> dict:
    """Map a hydrated central row + fusion score to the LOCAL _result_row shape."""
    return {
        "chunk_id": row["chunk_id"],
        "text": row["chunk_text"],
        "seq": row["seq"],
        "score": round(float(score), 6),
        "doc_id": row["document_id"],
        "title": row["title"],
        "source_type": row["source_type"],
        "published_at": row["published_at"],
        # created_at is the central equivalent of the local store's ingested_at.
        "ingested_at": _iso_or_none(row.get("created_at")),
        "source_path": row["source_path"],
        "provenance": row["provenance"],
        "embedding_model": row["embedding_model"],
        "meta": _coerce_meta(row.get("meta")),
    }


def _rerank_order(reranker: Reranker, query: str, results: List[dict]) -> List[dict]:
    """Reorder the WHOLE fused pool by cross-encoder score (mirrors local).

    Annotates every row with ``rerank_score`` (RRF ``score`` left intact); slicing
    and dedup are the caller's single tail step, so rerank composes with dedup.
    """
    if not results:
        return []
    scores = reranker.score(query, [r["text"] for r in results])
    if len(scores) != len(results):
        raise ValueError(
            f"reranker returned {len(scores)} scores for {len(results)} candidates"
        )
    order = sorted(range(len(results)), key=lambda i: (-scores[i], results[i]["chunk_id"]))
    reranked: List[dict] = []
    for i in order:
        r = dict(results[i])
        r["rerank_score"] = round(float(scores[i]), 6)
        reranked.append(r)
    return reranked


def _dedup_by_doc(results: List[dict], max_per_doc: int, top_k: int) -> List[dict]:
    """Keep at most ``max_per_doc`` chunks per ``doc_id``, then take ``top_k``.

    Backfills freed slots with further DISTINCT docs (mirrors local _dedup_by_doc).
    """
    counts: Dict[str, int] = {}
    kept: List[dict] = []
    for row in results:
        doc_id = row["doc_id"]
        if counts.get(doc_id, 0) >= max_per_doc:
            continue
        counts[doc_id] = counts.get(doc_id, 0) + 1
        kept.append(row)
        if len(kept) >= top_k:
            break
    return kept


def _finalize(
    fused: Sequence[tuple],
    rows_by_id: Dict[str, dict],
    query: str,
    rerank: bool,
    reranker: Optional[Reranker],
    max_per_doc: Optional[int],
    top_k: int,
) -> List[dict]:
    """Build result rows, then rerank (optional) then dedup/slice — local order."""
    results = [_result_row(rows_by_id[cid], score) for cid, score in fused if cid in rows_by_id]
    if rerank:
        assert reranker is not None  # narrowed by _resolve_pool_target; guards the checker
        results = _rerank_order(reranker, query, results)
    if max_per_doc is not None:
        return _dedup_by_doc(results, max_per_doc, top_k)
    return results[:top_k]


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
        with a DOUBLE cast, ``CAST(CAST(? AS NVARCHAR(MAX)) AS VECTOR(n))`` (param =
        ``json.dumps(floats)``). CORRECTION (card 4d362ffb): a single
        ``CAST(? AS VECTOR(n))`` was originally believed sufficient (card c8e686a9)
        but fails SQLSTATE 22018 on a realistic ~8KB embedding — pyodbc streams a
        parameter that long as SQL_WLONGVARCHAR, and SQL Server 2025 will not cast
        a streamed parameter directly to VECTOR. The NVARCHAR(MAX) hop avoids the
        streamed-cast path; ``json.dumps`` is still the correct param encoding (a
        fixed-decimal ``%.9f`` form was tried and fails).
        """
        sql = (
            "INSERT INTO memory.chunks "
            "(chunk_id, document_id, seq, chunk_text, meta, "
            "embedding_model, embedding_dim, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, "
            f"CAST(CAST(? AS NVARCHAR(MAX)) AS VECTOR({self.embedding_dim})))"
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

    # -- query (hybrid read path, Slice 2) --------------------------------- #
    def _doc_filter_sql(
        self,
        source_type: Optional[str],
        date_from: Optional[str],
        date_to: Optional[str],
    ) -> tuple[str, List[str]]:
        """Documents-table filter fragment (alias ``d``) + its params.

        Mirrors mcp/memory/store.py._doc_filter_sql. DATE-FALLBACK RECONCILIATION:
        the local store coalesces ``published_at`` with ``ingested_at``; central
        ``memory.documents`` has NO ``ingested_at`` column — ``created_at``
        (DATETIMEOFFSET) is its equivalent. It is rendered to its ``YYYY-MM-DD``
        prefix with ``CONVERT(char(10), ..., 23)`` (ISO style 23) so the
        first-10-chars STRING comparison matches the local ``substr(...,1,10)``
        semantics byte-for-byte (a plain lexical compare over zero-padded ISO
        dates is chronological under any collation).
        """
        clauses: List[str] = []
        params: List[str] = []
        if source_type:
            clauses.append("d.source_type = ?")
            params.append(source_type)
        date_expr = (
            "SUBSTRING(COALESCE(NULLIF(d.published_at, N''), "
            "CONVERT(char(10), d.created_at, 23)), 1, 10)"
        )
        if date_from:
            clauses.append(f"{date_expr} >= ?")
            params.append(date_from[:10])
        if date_to:
            clauses.append(f"{date_expr} <= ?")
            params.append(date_to[:10])
        return " AND ".join(clauses), params

    def _vector_candidate_ids(
        self, cur, query_vec: Sequence[float], pool: int, filt_sql: str, filt_params: List[str]
    ) -> List[str]:
        """Exact-scan vector kNN candidate chunk_ids, closest first.

        NO vector index by design (migration 008 header: exact VECTOR_DISTANCE scan
        under 50k vectors). The query vector binds via the SAME double-cast the
        INSERT uses (card 4d362ffb): ``CAST(CAST(? AS NVARCHAR(MAX)) AS VECTOR(n))``
        with the param = ``json.dumps(floats)`` — a realistic ~8KB vector streams as
        SQL_WLONGVARCHAR and a single cast fails SQLSTATE 22018. Cosine distance:
        smaller = closer, so ORDER BY ASC.
        """
        sql = (
            "SELECT TOP (?) c.chunk_id, "
            "VECTOR_DISTANCE('cosine', c.embedding, "
            f"CAST(CAST(? AS NVARCHAR(MAX)) AS VECTOR({self.embedding_dim}))) AS distance "
            "FROM memory.chunks c"
        )
        params: List[Any] = [pool, json.dumps([float(x) for x in query_vec])]
        if filt_sql:
            sql += (
                " JOIN memory.documents d ON d.document_id = c.document_id WHERE " + filt_sql
            )
            params += filt_params
        sql += " ORDER BY distance ASC"
        cur.execute(sql, *params)
        return [r["chunk_id"] for r in self._rows(cur)]

    def _lexical_candidate_ids(
        self,
        cur,
        contains_expr: str,
        terms: Sequence[str],
        pool: int,
        filt_sql: str,
        filt_params: List[str],
    ) -> List[str]:
        """Full-text CONTAINSTABLE candidate chunk_ids, re-ranked by pool-local BM25.

        CONTAINSTABLE returns ``[KEY]`` (the KEY INDEX value = ``chunk_seq_id``) and
        ``[RANK]`` (SQL Server's proprietary relevance, not BM25). The RANK-ordered
        query below is left UNCHANGED — it is still the candidate SOURCE (join back
        to chunks on ``chunk_seq_id``, ORDER BY RANK DESC; requires migration 009's
        full-text index). But CONTAINSTABLE RANK is a weaker top-of-list ordering
        than the local FTS5 ``bm25()``, which drops the fused nDCG@10 (card
        b81ef155). So we fetch the pool's ``chunk_text`` once and re-order the SAME
        ids with :func:`_bm25_rerank` (pool-local BM25). ROBUSTNESS: if there are no
        terms, no candidates, no text, or ANY scoring error, we fall back to the
        original RANK order — a ranking refinement must never crash a query (a
        lexical-arm failure must not 500 the endpoint).
        """
        sql = (
            "SELECT TOP (?) c.chunk_id "
            "FROM CONTAINSTABLE(memory.chunks, chunk_text, ?) AS kt "
            "JOIN memory.chunks c ON c.chunk_seq_id = kt.[KEY]"
        )
        params: List[Any] = [pool, contains_expr]
        if filt_sql:
            sql += (
                " JOIN memory.documents d ON d.document_id = c.document_id WHERE " + filt_sql
            )
            params += filt_params
        sql += " ORDER BY kt.[RANK] DESC"
        cur.execute(sql, *params)
        rank_ids = [r["chunk_id"] for r in self._rows(cur)]

        if not terms or not rank_ids:
            return rank_ids
        try:
            texts = self._load_chunk_texts(cur, rank_ids)
            if not texts:
                return rank_ids
            docs = [(cid, texts.get(cid, "")) for cid in rank_ids]
            return _bm25_rerank(terms, docs)
        except Exception:  # pragma: no cover - defensive; never fail a query on re-rank
            logger.exception(
                "BM25 lexical re-rank failed; falling back to CONTAINSTABLE RANK order"
            )
            return rank_ids

    def _load_chunk_texts(self, cur, chunk_ids: Sequence[str]) -> Dict[str, str]:
        """Fetch ``chunk_text`` for a set of chunk_ids (batched IN-list).

        Feeds the BM25 lexical re-rank (:func:`_bm25_rerank`): the CONTAINSTABLE
        candidate SQL returns only ids ordered by RANK, so the pool's text must be
        read once to rescore it. Batched exactly like :meth:`_load_candidate_rows`
        (``_IN_CHUNK`` per statement); the returned map is unordered — the caller
        re-imposes the RANK order.
        """
        texts: Dict[str, str] = {}
        ids = list(chunk_ids)
        if not ids:
            return texts
        for batch in _chunked(ids):
            placeholders = ",".join("?" * len(batch))
            cur.execute(
                "SELECT chunk_id, chunk_text FROM memory.chunks "
                f"WHERE chunk_id IN ({placeholders})",
                *batch,
            )
            for r in self._rows(cur):
                texts[r["chunk_id"]] = r["chunk_text"]
        return texts

    def _load_candidate_rows(self, cur, chunk_ids: Sequence[str]) -> Dict[str, dict]:
        """Hydrate the fused chunk_ids with document metadata (batched IN-list).

        Mirrors mcp/memory/store.py._load_candidate_rows; central column names
        (chunk_text, document_id, created_at) are mapped to the local row shape by
        :func:`_result_row`.
        """
        rows: Dict[str, dict] = {}
        ids = list(chunk_ids)
        if not ids:
            return rows
        for batch in _chunked(ids):
            placeholders = ",".join("?" * len(batch))
            cur.execute(
                "SELECT c.chunk_id, c.chunk_text, c.seq, c.meta, c.embedding_model, "
                "d.document_id, d.title, d.source_type, d.published_at, "
                "d.created_at, d.source_path, d.provenance "
                "FROM memory.chunks c JOIN memory.documents d "
                "ON d.document_id = c.document_id "
                f"WHERE c.chunk_id IN ({placeholders})",
                *batch,
            )
            for r in self._rows(cur):
                rows[r["chunk_id"]] = r
        return rows

    def query(
        self,
        query: str,
        embedder: Embedder,
        *,
        top_k: int = 8,
        source_type: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        rerank: bool = False,
        rerank_pool: Optional[int] = None,
        max_per_doc: Optional[int] = None,
        reranker: Optional[Reranker] = None,
    ) -> List[dict]:
        """Hybrid vector-kNN + full-text retrieval fused with RRF (k=60).

        Reproduces the LOCAL store's query semantics (mcp/memory/store.py
        MemoryStore.query) against SQL Server: the same validation, pool sizing,
        source_type / date filters (pushed into both candidate queries), RRF
        fusion, optional cross-encoder rerank, and optional per-document dedup
        (``max_per_doc``), in the same fuse → rerank → dedup order, returning the
        same list-of-row-dicts shape. One short-lived read connection (per-op
        cursor), mirroring the write methods' connection discipline.
        """
        date_from, date_to = _validate_query_args(
            embedder, self.embedding_dim, top_k, date_from, date_to, source_type, max_per_doc
        )
        pool_target = _resolve_pool_target(top_k, rerank, reranker, rerank_pool, max_per_doc)
        pool = _candidate_pool(pool_target)

        query_vec = embedder.embed_query(query)
        contains_expr = _contains_query(query)
        lexical_terms = _query_terms(query)
        filt_sql, filt_params = self._doc_filter_sql(source_type, date_from, date_to)

        conn = self._connect()
        try:
            cur = conn.cursor()
            vec_ids = self._vector_candidate_ids(cur, query_vec, pool, filt_sql, filt_params)
            fts_ids: List[str] = []
            if contains_expr:
                fts_ids = self._lexical_candidate_ids(
                    cur, contains_expr, lexical_terms, pool, filt_sql, filt_params
                )
            fused = fuse_ranked_ids(vec_ids, fts_ids, top_k=pool_target, k=RRF_K)
            rows_by_id = self._load_candidate_rows(cur, [cid for cid, _ in fused])
        finally:
            conn.close()

        return _finalize(fused, rows_by_id, query, rerank, reranker, max_per_doc, top_k)

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

    # -- query (hybrid read path, Slice 2) --------------------------------- #
    def _allowed_doc_ids(
        self,
        source_type: Optional[str],
        date_from: Optional[str],
        date_to: Optional[str],
    ) -> Optional[set]:
        """Doc ids passing the source_type / date filters, or None (no filter).

        Reproduces :meth:`MemoryStore._doc_filter_sql` semantics in Python: the
        date compared is the first 10 chars of ``published_at`` (falling back to
        ``created_at``), and an empty/missing date is excluded from a date-bounded
        query — the local ``substr(COALESCE(...), 1, 10)`` behavior.
        """
        if not source_type and not date_from and not date_to:
            return None
        allowed: set = set()
        for did, doc in self.documents.items():
            if source_type and doc.get("source_type") != source_type:
                continue
            date_val = doc.get("published_at") or ""
            if not date_val:
                date_val = _iso_or_none(doc.get("created_at")) or ""
            prefix = date_val[:10]
            if date_from and not (prefix and prefix >= date_from[:10]):
                continue
            if date_to and not (prefix and prefix <= date_to[:10]):
                continue
            allowed.add(did)
        return allowed

    def _fake_vector_ids(
        self, query_vec: Sequence[float], pool: int, allowed: Optional[set]
    ) -> List[str]:
        """Real cosine-distance kNN over the in-memory vectors, closest first."""
        scored = []
        for cid, vec in self.vectors.items():
            chunk = self.chunks.get(cid)
            if chunk is None:
                continue
            if allowed is not None and chunk["document_id"] not in allowed:
                continue
            scored.append((_cosine_distance(query_vec, vec), cid))
        # distance ASC, deterministic chunk_id tie-break (SQL leaves ties unordered).
        scored.sort(key=lambda t: (t[0], t[1]))
        return [cid for _, cid in scored[:pool]]

    def _fake_lexical_ids(
        self, terms: Sequence[str], pool: int, allowed: Optional[set]
    ) -> List[str]:
        """Naive case-insensitive substring lexical match, most matches first."""
        hits = []
        for cid, chunk in self.chunks.items():
            if allowed is not None and chunk["document_id"] not in allowed:
                continue
            text = (chunk.get("chunk_text") or "").lower()
            score = sum(1 for t in terms if t in text)
            if score > 0:
                hits.append((score, cid))
        # more matches first, deterministic chunk_id tie-break.
        hits.sort(key=lambda t: (-t[0], t[1]))
        return [cid for _, cid in hits[:pool]]

    def _fake_hydrate(self, chunk_ids: Sequence[str]) -> Dict[str, dict]:
        """Assemble central-column-shaped rows so _result_row consumes them as-is."""
        rows: Dict[str, dict] = {}
        for cid in chunk_ids:
            chunk = self.chunks.get(cid)
            if chunk is None:
                continue
            doc = self.documents.get(chunk["document_id"], {})
            rows[cid] = {
                "chunk_id": chunk["chunk_id"],
                "chunk_text": chunk.get("chunk_text"),
                "seq": chunk.get("seq"),
                "meta": chunk.get("meta"),
                "embedding_model": chunk.get("embedding_model"),
                "document_id": chunk["document_id"],
                "title": doc.get("title"),
                "source_type": doc.get("source_type"),
                "published_at": doc.get("published_at"),
                "created_at": doc.get("created_at"),
                "source_path": doc.get("source_path"),
                "provenance": doc.get("provenance"),
            }
        return rows

    def query(
        self,
        query: str,
        embedder: Embedder,
        *,
        top_k: int = 8,
        source_type: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        rerank: bool = False,
        rerank_pool: Optional[int] = None,
        max_per_doc: Optional[int] = None,
        reranker: Optional[Reranker] = None,
    ) -> List[dict]:
        """In-memory parity of :meth:`MemoryStore.query` — real cosine + substring
        lexical, the SAME validation / pool sizing / fusion / finalize path, so the
        endpoint tests exercise the full read path with NO SQL Server."""
        date_from, date_to = _validate_query_args(
            embedder, self.embedding_dim, top_k, date_from, date_to, source_type, max_per_doc
        )
        pool_target = _resolve_pool_target(top_k, rerank, reranker, rerank_pool, max_per_doc)
        pool = _candidate_pool(pool_target)

        query_vec = embedder.embed_query(query)
        allowed = self._allowed_doc_ids(source_type, date_from, date_to)
        vec_ids = self._fake_vector_ids(query_vec, pool, allowed)
        terms = [t.lower() for t in _query_terms(query)]
        fts_ids = self._fake_lexical_ids(terms, pool, allowed) if terms else []

        fused = fuse_ranked_ids(vec_ids, fts_ids, top_k=pool_target, k=RRF_K)
        rows_by_id = self._fake_hydrate([cid for cid, _ in fused])
        return _finalize(fused, rows_by_id, query, rerank, reranker, max_per_doc, top_k)

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
