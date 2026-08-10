"""Unit tests for memory.retrieval (Reciprocal Rank Fusion).

Pure-Python; runs everywhere without the optional memory extras.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from memory_core import retrieval  # noqa: E402


class TestReciprocalRankFusion(unittest.TestCase):
    def test_scores_match_formula(self):
        fused = dict(retrieval.reciprocal_rank_fusion([["a", "b"], ["b", "c"]], k=60))
        self.assertAlmostEqual(fused["a"], 1 / 61)
        self.assertAlmostEqual(fused["b"], 1 / 62 + 1 / 61)
        self.assertAlmostEqual(fused["c"], 1 / 62)

    def test_item_in_both_lists_ranks_first(self):
        # "b" appears in both lists, so it should outrank list-1-only "a".
        ranked = retrieval.reciprocal_rank_fusion([["a", "b"], ["b", "d"]])
        self.assertEqual(ranked[0][0], "b")

    def test_deterministic_tie_break_by_id(self):
        ranked = retrieval.reciprocal_rank_fusion([["x"], ["y"]])
        # Both have identical score 1/61 -> tie broken by id ascending.
        self.assertEqual([r[0] for r in ranked], ["x", "y"])

    def test_empty_lists(self):
        self.assertEqual(retrieval.reciprocal_rank_fusion([]), [])
        self.assertEqual(retrieval.reciprocal_rank_fusion([[], []]), [])

    def test_k_changes_weighting(self):
        low_k = dict(retrieval.reciprocal_rank_fusion([["a", "b"]], k=1))
        high_k = dict(retrieval.reciprocal_rank_fusion([["a", "b"]], k=1000))
        # Smaller k amplifies the gap between rank 1 and rank 2.
        self.assertGreater(low_k["a"] - low_k["b"], high_k["a"] - high_k["b"])


class TestFuseRankedIds(unittest.TestCase):
    def test_top_k_truncates(self):
        fused = retrieval.fuse_ranked_ids(["a", "b", "c"], ["c", "d"], top_k=2)
        self.assertEqual(len(fused), 2)

    def test_top_k_zero_returns_all(self):
        fused = retrieval.fuse_ranked_ids(["a", "b"], ["b", "c"], top_k=0)
        self.assertEqual({item_id for item_id, _ in fused}, {"a", "b", "c"})


if __name__ == "__main__":
    unittest.main()
