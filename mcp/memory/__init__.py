"""Central memory subsystem (Tier 0) for agent-os.

A fully-local, machine-global vector + full-text corpus store: ingest documents
(md/txt), ChatGPT conversation exports, and dated news; chunk and embed them;
retrieve with hybrid BM25 (FTS5) + vector kNN (sqlite-vec) fused via RRF.

This subsystem is OPTIONAL, exactly like the SQL-Server database-graph
subsystem.  The MCP server must import :mod:`memory_tools` and start with the
heavy dependencies (``sqlite-vec``, ``sentence-transformers``, a
loadable-extension-capable SQLite) absent; the tools then report the subsystem
as unavailable instead of raising.  Only the pure-Python helpers here
(:mod:`memory.chunking`, :mod:`memory.adapters`, :mod:`memory.retrieval`) import
cleanly without those dependencies.
"""

from .availability import (
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_RERANKER_MODEL,
    MemoryUnavailable,
    check_availability,
    default_db_path,
)

__all__ = [
    "DEFAULT_EMBEDDING_DIM",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_RERANKER_MODEL",
    "MemoryUnavailable",
    "check_availability",
    "default_db_path",
]
