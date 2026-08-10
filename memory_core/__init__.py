"""Shared, model-free retrieval core for the central memory subsystem.

This top-level package (sibling to ``contracts`` and ``database``) holds the
reusable, storage-agnostic pieces of the memory subsystem so that BOTH the
local-SQLite store (``mcp/memory/store.py``) and the future SQL-Server-backed
API store (``services/api``) import the EXACT same code — no re-implementation,
no behavioral drift:

  * :mod:`memory_core.chunking`   — prose + conversation chunking (pure Python).
  * :mod:`memory_core.adapters`   — source adapters (md/txt, ChatGPT export).
  * :mod:`memory_core.retrieval`  — Reciprocal Rank Fusion for hybrid retrieval.
  * :mod:`memory_core.embeddings` — the :class:`Embedder` protocol + the
    sentence-transformers backend (heavy import is lazy).
  * :mod:`memory_core.reranker`   — the :class:`Reranker` protocol + the
    cross-encoder backend (heavy import is lazy).
  * :mod:`memory_core.availability` — dependency detection, SQLite backend
    resolution, and shared paths/constants.

Like the SQL-Server database-graph subsystem, the heavy dependencies
(``sqlite-vec``, ``sentence-transformers``, a loadable-extension-capable SQLite)
are OPTIONAL: importing any module here is always safe without them — the pure
helpers (:mod:`memory_core.chunking`, :mod:`memory_core.adapters`,
:mod:`memory_core.retrieval`) import cleanly, and the embedder/reranker keep
their model imports lazy so they too import without the extras present.
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
