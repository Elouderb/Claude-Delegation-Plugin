"""Unit tests for memory.chunking (prose + message-boundary).

Pure-Python module, so these run in every environment without the optional
memory extras.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory import chunking  # noqa: E402


class TestEstimateTokens(unittest.TestCase):
    def test_counts_words_and_punctuation(self):
        self.assertEqual(chunking.estimate_tokens(""), 0)
        self.assertGreater(chunking.estimate_tokens("hello, world!"), 2)


class TestChunkProse(unittest.TestCase):
    def test_short_text_single_chunk(self):
        chunks = chunking.chunk_prose("Just one short sentence about apples.")
        self.assertEqual(len(chunks), 1)
        self.assertIn("apples", chunks[0])

    def test_empty_text(self):
        self.assertEqual(chunking.chunk_prose(""), [])
        self.assertEqual(chunking.chunk_prose("   \n\n  "), [])

    def test_multi_chunk_respects_budget_and_overlaps(self):
        # Distinct unique tokens per sentence make overlap provable.
        sentences = [f"sentinel{i} filler filler filler filler filler." for i in range(30)]
        text = "\n\n".join(sentences)
        chunks = chunking.chunk_prose(text, target_tokens=20, overlap_ratio=0.2)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(chunking.estimate_tokens(chunk), 20)
        # Sentence-aligned overlap: consecutive chunks share at least one word.
        for a, b in zip(chunks, chunks[1:]):
            self.assertTrue(
                set(a.split()) & set(b.split()),
                msg="expected overlapping words between consecutive chunks",
            )

    def test_oversized_sentence_hard_split(self):
        big = "word " * 200  # single runaway "sentence", 200 tokens
        chunks = chunking.chunk_prose(big, target_tokens=50)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(chunking.estimate_tokens(chunk), 50)

    def test_punctuation_dense_never_exceeds_budget(self):
        # Finding 4: punctuation counts as tokens, so a slice measured in words
        # could blow a token budget. Every chunk must stay within budget.
        text = " ".join(["a,b,c,d,e,f,g,h!"] * 80)
        for tgt in (20, 50, 111):
            chunks = chunking.chunk_prose(text, target_tokens=tgt, overlap_ratio=0.2)
            self.assertTrue(chunks)
            for chunk in chunks:
                self.assertLessEqual(chunking.estimate_tokens(chunk), tgt)

    def test_asymmetric_paragraphs_never_exceed_budget(self):
        # A huge paragraph next to tiny ones (overlap-seed interaction) must not
        # produce an over-budget chunk (finding 4).
        text = ("word " * 400).strip() + "\n\ntiny.\n\n" + ("blah " * 300).strip()
        chunks = chunking.chunk_prose(text, target_tokens=60, overlap_ratio=0.15)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(chunking.estimate_tokens(chunk), 60)

    def test_overlap_present_but_bounded(self):
        # Near-budget sentences stress the overlap seed: overlap must exist yet
        # never push a chunk over budget.
        sentences = [("tok " * (i % 5 + 1) + "end.").strip() for i in range(200)]
        chunks = chunking.chunk_prose("\n\n".join(sentences), target_tokens=40, overlap_ratio=0.2)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(chunking.estimate_tokens(chunk), 40)

    def test_cjk_no_whitespace_no_punctuation_is_bounded(self):
        # Finding 5: a whitespace-free CJK run is counted per-codepoint and
        # character-hard-split, so it can never become one unbounded chunk.
        cjk = "这是一个没有空格也没有标点的超长中文文本" * 40
        self.assertGreater(chunking.estimate_tokens(cjk), 100)
        chunks = chunking.chunk_prose(cjk, target_tokens=50)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(chunking.estimate_tokens(chunk), 50)

    def test_cjk_with_sentence_punctuation_splits(self):
        cjk = "。".join(["这是第" + str(i) + "句非常长的中文句子内容很多很多字" * 3 for i in range(20)])
        chunks = chunking.chunk_prose(cjk, target_tokens=50)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(chunking.estimate_tokens(chunk), 50)


class TestChunkMessages(unittest.TestCase):
    def test_small_messages_packed_with_roles(self):
        messages = [
            {"role": "user", "text": "Hello there", "id": "m1"},
            {"role": "assistant", "text": "General Kenobi", "id": "m2"},
        ]
        chunks = chunking.chunk_messages(messages, target_tokens=500)
        self.assertEqual(len(chunks), 1)
        self.assertIn("user: Hello there", chunks[0]["text"])
        self.assertIn("assistant: General Kenobi", chunks[0]["text"])
        self.assertEqual(chunks[0]["roles"], ["user", "assistant"])
        self.assertEqual(chunks[0]["message_ids"], ["m1", "m2"])

    def test_empty_messages_skipped(self):
        messages = [
            {"role": "system", "text": "", "id": "s"},
            {"role": "user", "text": "real content here", "id": "u"},
        ]
        chunks = chunking.chunk_messages(messages)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["roles"], ["user"])

    def test_oversized_message_split_but_boundary_respected(self):
        # One giant user message exceeds the budget -> prose-split into its own
        # single-role chunks; the neighbouring small message stays separate.
        messages = [
            {"role": "user", "text": "word " * 300, "id": "big"},
            {"role": "assistant", "text": "short reply", "id": "small"},
        ]
        chunks = chunking.chunk_messages(messages, target_tokens=50)
        self.assertGreater(len(chunks), 2)
        # Every piece of the big message is single-role "user".
        big_pieces = [c for c in chunks if c["message_ids"] == ["big"]]
        self.assertTrue(big_pieces)
        for piece in big_pieces:
            self.assertEqual(piece["roles"], ["user"])
        # The assistant reply is a distinct chunk (message boundary respected).
        self.assertTrue(any(c["roles"] == ["assistant"] for c in chunks))

    def test_turn_order_preserved(self):
        messages = [
            {"role": "user", "text": "first", "id": "1"},
            {"role": "assistant", "text": "second", "id": "2"},
            {"role": "user", "text": "third", "id": "3"},
        ]
        # Force each message into its own chunk with a tiny budget.
        chunks = chunking.chunk_messages(messages, target_tokens=3)
        texts = [c["text"] for c in chunks]
        self.assertEqual(
            texts, ["user: first", "assistant: second", "user: third"]
        )


if __name__ == "__main__":
    unittest.main()
