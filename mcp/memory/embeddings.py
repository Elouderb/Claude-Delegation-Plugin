"""Embedding backends.

The default backend is ``sentence-transformers`` running bge-small-en-v1.5
(384-dim) fully locally: the model downloads once and is cached, after which all
encoding is offline.  The heavy import is lazy (inside the class), so importing
this module is always cheap and safe even when sentence-transformers is absent.

The store and retrieval layers depend only on the small :class:`Embedder`
structural protocol below, which lets tests inject a deterministic stub with
recorded vectors instead of loading a real model.
"""

from __future__ import annotations

import functools
import os
from typing import Any, List, Protocol, cast

from .availability import (
    _INSTALL_HINT,
    BGE_QUERY_PROMPT,
    DEFAULT_EMBEDDING_MODEL,
    MemoryUnavailable,
)

# Env override for the sentence-transformers backend ("torch" default, or
# "onnx" / "openvino").  ONNX is preferred where practical but pulls extra
# extras (optimum/onnxruntime); leaving this unset keeps the default backend.
_BACKEND_ENV = "AGENT_OS_MEMORY_ST_BACKEND"
_ALLOWED_BACKENDS = ("torch", "onnx", "openvino")


class Embedder(Protocol):
    """Minimal embedding interface the store/retrieval layers depend on."""

    @property
    def model_name(self) -> str:  # e.g. "BAAI/bge-small-en-v1.5"
        ...

    @property
    def dim(self) -> int:
        ...

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        ...

    def embed_query(self, text: str) -> List[float]:
        ...


class SentenceTransformerEmbedder:
    """bge-small-en-v1.5 via sentence-transformers, embeddings L2-normalized.

    Vectors are normalized so the store's default L2 vec0 ranking matches cosine
    similarity ordering.  The bge query instruction is applied to queries only
    (passages are embedded verbatim), per the model card.
    """

    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - exercised via availability
            raise MemoryUnavailable(
                f"sentence-transformers is not installed. {_INSTALL_HINT}"
            ) from exc

        self._model_name = model_name
        backend = os.environ.get(_BACKEND_ENV, "").strip().lower()
        if backend and backend not in _ALLOWED_BACKENDS:
            raise MemoryUnavailable(
                f"invalid {_BACKEND_ENV}={backend!r}; expected one of {_ALLOWED_BACKENDS}"
            )
        # Branch explicitly rather than **kwargs-unpack so the backend argument
        # is passed by keyword unambiguously.  ``backend`` is validated above; the
        # cast keeps the checker happy about the env-sourced (runtime) value.
        if backend:
            self._model = SentenceTransformer(model_name, backend=cast(Any, backend))
        else:
            self._model = SentenceTransformer(model_name)

        dim = self._model.get_sentence_embedding_dimension()
        if dim is None:
            raise MemoryUnavailable(
                f"could not determine embedding dimension for model {model_name!r}"
            )
        self._dim = int(dim)

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dim(self) -> int:
        return self._dim

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        vectors = self._model.encode(
            list(texts), normalize_embeddings=True, convert_to_numpy=True
        )
        return [list(map(float, v)) for v in vectors]

    def embed_query(self, text: str) -> List[float]:
        vector = self._model.encode(
            text, prompt=BGE_QUERY_PROMPT, normalize_embeddings=True, convert_to_numpy=True
        )
        return list(map(float, vector))


@functools.lru_cache(maxsize=1)
def get_default_embedder() -> Embedder:
    """Return the process-wide default (bge-small-en-v1.5) embedder.

    Memoized so the model is loaded from disk once per process instead of on
    every ``memory_ingest`` / ``memory_query`` call.  The cache also gives tests
    a clean seam: patch this function (or call ``get_default_embedder.cache_clear()``)
    to inject a deterministic stub without loading torch.
    """
    return SentenceTransformerEmbedder()
