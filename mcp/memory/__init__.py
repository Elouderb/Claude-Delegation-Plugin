"""Local-SQLite memory implementation (the repo-local half of the subsystem).

The shared, model-free retrieval core (chunking, adapters, RRF fusion, the
Embedder/Reranker protocol wrappers, availability plumbing) now lives in the
top-level :mod:`memory_core` package so ``services/api`` can import the exact
same code.  What remains here is local-specific: :mod:`memory.store`, the
machine-global ``memory.sqlite`` implementation the MCP ``memory_*`` tools use.
"""
