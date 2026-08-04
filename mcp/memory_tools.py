"""Central-memory MCP tool implementations: memory_ingest / memory_query /
memory_status.

Registered on the same FastMCP("task-cards") server as the card and graph
tools.  This subsystem is OPTIONAL: importing this module never pulls the heavy
dependencies (sqlite-vec, sentence-transformers, pysqlite3), and every tool
checks availability first — if the deps are absent the tool returns a clear
"unavailable" result instead of raising, exactly as the SQL-Server db_* tools
degrade without pyodbc.  So the server starts and all 24 existing tools keep
working whether or not the memory extras are installed.

Error handling follows the graph_io precedent: input-validation errors are
surfaced verbatim, but any unexpected exception is logged in full and reduced to
a generic message in the tool response (no internal disclosure).  All three
tools share a uniform result contract (``available`` + ``operation``, plus
``error`` / ``missing`` / ``hint`` when relevant) so callers can branch safely.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from graph_io import log
from memory.availability import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    MemoryUnavailable,
    check_availability,
    default_db_path,
)

# Upper bound on top_k at the tool boundary (the store additionally clamps the
# candidate pool to sqlite-vec's 4096 kNN limit).
_MAX_TOP_K = 1000


def _unavailable_result(availability: dict, operation: str, detail: Optional[str] = None) -> dict:
    """Uniform 'memory subsystem unavailable' payload for a blocked operation."""
    return {
        "available": False,
        "operation": operation,
        "error": detail or availability["hint"],
        "missing": availability["missing"],
        "hint": availability["hint"],
    }


def _error_result(operation: str, message: str, error_extra: Optional[dict] = None) -> dict:
    """Uniform error payload (deps present, but the operation failed)."""
    return {**(error_extra or {}), "available": True, "operation": operation, "error": message}


def _new_store():
    """Construct a MemoryStore at the central path (imports the store lazily)."""
    from memory.store import MemoryStore

    return MemoryStore(default_db_path(), embedding_dim=DEFAULT_EMBEDDING_DIM)


def _run(operation: str, func: Callable[[], dict], error_extra: Optional[dict] = None) -> dict:
    """Run a memory operation with availability gating + sanitized error shaping."""
    availability = check_availability()
    if not availability["available"]:
        return _unavailable_result(availability, operation)
    try:
        return func()
    except MemoryUnavailable as exc:
        # A dependency find_spec saw may have failed to import for real (e.g. a
        # broken torch); re-check and report as unavailable.
        return _unavailable_result(check_availability(), operation, detail=str(exc))
    except (ValueError, FileNotFoundError) as exc:
        # Caller-input / not-found errors are safe to surface verbatim.
        return _error_result(operation, str(exc), error_extra)
    except Exception as exc:  # unexpected: log fully, disclose nothing
        log(f"ERROR in {operation}: {exc}")
        return _error_result(operation, "internal error; see server logs", error_extra)


def memory_ingest(
    path: str,
    source_type: Optional[str] = None,
    published_at: Optional[str] = None,
) -> dict:
    """Ingest a file or directory into the central memory store.

    Auto-detects the adapter per file: a ChatGPT ``conversations.json`` export
    vs. plain Markdown/text.  ``source_type`` overrides the prose type
    (``document`` default, or ``news`` which requires ``published_at``);
    ``chatgpt_conversation`` is adapter-assigned and not a valid caller value.
    Content-hash dedup makes re-ingesting an unchanged file a no-op; a changed
    file replaces its chunks; a metadata-only change updates the document row; an
    emptied known file is removed.  Per-file parse failures are reported in
    ``files_failed`` without aborting a directory walk.
    """

    def _do() -> dict:
        from memory.embeddings import get_default_embedder

        store = _new_store()
        embedder = get_default_embedder()
        summary = store.ingest_path(
            path, embedder, source_type=source_type, published_at=published_at
        )
        summary["available"] = True
        summary["operation"] = "memory_ingest"
        log(
            f"memory_ingest {path}: +{summary['documents_new']} new / "
            f"{summary['documents_replaced']} replaced / "
            f"{summary['documents_unchanged']} unchanged / "
            f"{summary['documents_metadata_updated']} meta / "
            f"{summary['documents_removed']} removed, "
            f"{summary['chunks_written']} chunks"
        )
        return summary

    return _run("memory_ingest", _do)


def memory_query(
    query: str,
    top_k: int = 8,
    source_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """Hybrid retrieval over the central memory store.

    Fuses BM25 (FTS5) and vector kNN (sqlite-vec) with Reciprocal Rank Fusion
    (k=60).  Optional filters: ``source_type`` and a ``date_from``/``date_to``
    range (matched against ``published_at``, falling back to ``ingested_at``).
    Returns the top_k chunks with text, ids, document metadata, and fusion score.
    """
    error_extra = {"query": query, "top_k": top_k, "count": 0, "results": []}

    def _do() -> dict:
        if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1 or top_k > _MAX_TOP_K:
            raise ValueError(f"top_k must be an integer in 1..{_MAX_TOP_K}, got {top_k!r}")
        from memory.embeddings import get_default_embedder

        store = _new_store()
        embedder = get_default_embedder()
        results = store.query(
            embedder,
            query,
            top_k=top_k,
            source_type=source_type,
            date_from=date_from,
            date_to=date_to,
        )
        log(f"memory_query {query!r}: {len(results)} results")
        return {
            "available": True,
            "operation": "memory_query",
            "query": query,
            "top_k": top_k,
            "count": len(results),
            "results": results,
        }

    return _run("memory_query", _do, error_extra=error_extra)


def memory_status() -> dict:
    """Report memory-subsystem availability, corpus stats, model, and DB info.

    Side-effect-free: it never creates the database (corpus stats are read only
    when the file already exists) and computes ``db_exists`` / ``db_size_bytes``
    from a single ``stat`` call (no exists()/stat() TOCTOU).  Always returns
    something useful — even with the optional deps absent it reports what is
    missing and the (possibly non-existent) database path.
    """
    availability = check_availability()
    db_path = default_db_path()
    try:
        stat = os.stat(db_path)
        db_exists, db_size = True, stat.st_size
    except OSError:
        db_exists, db_size = False, 0

    status = {
        **availability,
        "operation": "memory_status",
        "embedding_model": DEFAULT_EMBEDDING_MODEL,
        "embedding_dim": DEFAULT_EMBEDDING_DIM,
        "db_path": str(db_path),
        "db_exists": db_exists,
        "db_size_bytes": db_size,
    }
    # Only read stats when the DB already exists, so status never creates it.
    if availability["storage_available"] and db_exists:
        try:
            status["corpus"] = _new_store().stats()
        except Exception as exc:  # pragma: no cover - defensive
            log(f"ERROR in memory_status stats: {exc}")
            status["corpus_error"] = "internal error; see server logs"
    return status
