"""Cross-encoder reranker backend (Phase 3a, ranking-quality experiment).

A reranker refines the ORDER of already-retrieved hybrid candidates: hybrid RRF
retrieves a larger candidate pool, a cross-encoder scores each ``(query, chunk)``
pair jointly (unlike the bi-encoder embedder, which scores query and chunk
separately), and the top_k by that score are returned.  Cross-encoders are more
accurate at relevance ranking but heavier per query, which is exactly what the
go/no-go gate measures.

This module deliberately mirrors :mod:`memory.embeddings`:

  * the heavy import (``sentence_transformers.CrossEncoder``) is lazy — done
    inside the class, never at module import — so importing this module is always
    cheap and safe even when sentence-transformers is absent;
  * an absent dependency (or a failed model download) raises
    :class:`MemoryUnavailable`, which the tool layer converts into a clean
    "reranker unavailable" result rather than a crash;
  * the store/eval layers depend only on the small :class:`Reranker` structural
    protocol below, so tests inject a deterministic stub instead of loading a
    real model;
  * :func:`get_default_reranker` is memoized (``lru_cache``) so the model loads
    from disk once per process, exactly like ``get_default_embedder``.

The reranker is NOT a new dependency: it runs on the SAME sentence-transformers
runtime as the embedder, so there is no separate requirements file — only an
additional small model download, cached once and then fully offline.
"""

from __future__ import annotations

import functools
import os
from typing import List, Protocol, Sequence

from .availability import (
    _INSTALL_HINT,
    DEFAULT_RERANKER_MODEL,
    MemoryUnavailable,
)

# Env override for the reranker model name, so the empirically-chosen model can
# be swapped in (e.g. bge-reranker-base) without a code change.
_MODEL_ENV = "AGENT_OS_MEMORY_RERANKER_MODEL"

# Env override for the cross-encoder scoring batch size (pairs per forward pass).
_BATCH_ENV = "AGENT_OS_MEMORY_RERANK_BATCH"
_DEFAULT_BATCH = 32


class Reranker(Protocol):
    """Minimal reranking interface the store/eval layers depend on."""

    @property
    def model_name(self) -> str:  # e.g. "cross-encoder/ms-marco-MiniLM-L-6-v2"
        ...

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        """Return one relevance score per text, aligned to ``texts`` order."""
        ...


def _resolve_batch_size() -> int:
    raw = os.environ.get(_BATCH_ENV, "").strip()
    if not raw:
        return _DEFAULT_BATCH
    try:
        value = int(raw)
    except ValueError:
        raise MemoryUnavailable(f"invalid {_BATCH_ENV}={raw!r}; expected a positive integer")
    if value < 1:
        raise MemoryUnavailable(f"invalid {_BATCH_ENV}={raw!r}; expected a positive integer")
    return value


class CrossEncoderReranker:
    """A local sentence-transformers ``CrossEncoder`` reranker.

    ``score(query, texts)`` returns one float per text — higher is more relevant
    — in the SAME order as ``texts`` (``CrossEncoder.predict`` un-sorts its
    internal length-batching back to input order), so the caller can re-associate
    scores with candidate rows and reorder deterministically.
    """

    def __init__(self, model_name: str = DEFAULT_RERANKER_MODEL, batch_size: int | None = None):
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover - exercised via availability
            raise MemoryUnavailable(
                f"sentence-transformers is not installed. {_INSTALL_HINT}"
            ) from exc

        self._model_name = model_name
        self._batch_size = int(batch_size) if batch_size is not None else _resolve_batch_size()
        if self._batch_size < 1:
            raise MemoryUnavailable(f"batch_size must be >= 1, got {self._batch_size}")
        try:
            self._model = CrossEncoder(model_name)
        except Exception as exc:  # model download / load failure -> clean unavailable
            raise MemoryUnavailable(
                f"reranker model {model_name!r} could not be loaded ({exc}). "
                "It downloads once from Hugging Face on first use, then runs offline; "
                "check connectivity or the model name."
            ) from exc

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def batch_size(self) -> int:
        return self._batch_size

    def score(self, query: str, texts: Sequence[str]) -> List[float]:
        texts = list(texts)
        if not texts:
            return []
        pairs = [(query, text) for text in texts]
        scores = self._model.predict(
            pairs,
            batch_size=self._batch_size,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [float(s) for s in scores]


@functools.lru_cache(maxsize=1)
def get_default_reranker() -> Reranker:
    """Return the process-wide default cross-encoder reranker.

    The model name comes from ``AGENT_OS_MEMORY_RERANKER_MODEL`` when set, else
    :data:`DEFAULT_RERANKER_MODEL`.  Memoized so the (heavy) model loads once per
    process; tests patch this function or call
    ``get_default_reranker.cache_clear()`` to inject a stub without loading torch.
    """
    model_name = os.environ.get(_MODEL_ENV, "").strip() or DEFAULT_RERANKER_MODEL
    return CrossEncoderReranker(model_name)
