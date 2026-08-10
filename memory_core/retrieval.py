"""Reciprocal Rank Fusion (RRF) for hybrid retrieval.

Pure Python — no heavy dependencies — so it is unit-testable in isolation.  The
store layer produces two ranked candidate lists (vector kNN and BM25/FTS5) and
fuses them here; RRF needs only rank order, not comparable scores, which is why
it is robust to mixing L2 distances with BM25 scores.
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

# Fusion constant from the RRF paper / the epic's validated decision.
RRF_K = 60


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[str]],
    k: int = RRF_K,
) -> List[Tuple[str, float]]:
    """Fuse ranked id lists into one ranking by summed reciprocal ranks.

    ``ranked_lists`` is a sequence of id sequences, each ordered best-first.
    Every list contributes ``1 / (k + rank)`` (rank is 1-based) to each id it
    contains; contributions are summed across lists.  Returns ``(id, score)``
    pairs sorted by score descending, ties broken by id for determinism.
    """
    scores: Dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, item_id in enumerate(ranked, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def fuse_ranked_ids(
    vector_ids: Sequence[str],
    keyword_ids: Sequence[str],
    top_k: int,
    k: int = RRF_K,
) -> List[Tuple[str, float]]:
    """Convenience wrapper: fuse the vector and keyword lists, keep ``top_k``."""
    fused = reciprocal_rank_fusion([vector_ids, keyword_ids], k=k)
    return fused[:top_k] if top_k and top_k > 0 else fused
