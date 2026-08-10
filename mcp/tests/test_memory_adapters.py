"""Unit tests for memory.adapters (document + ChatGPT export parsing).

Pure-Python; runs everywhere without the optional memory extras.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from memory_core.adapters import base, chatgpt, documents  # noqa: E402

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "chatgpt_conversations.json"


class TestChatGPTAdapter(unittest.TestCase):
    def setUp(self):
        self.docs = chatgpt.load(_FIXTURE)
        self.by_title = {d.title: d for d in self.docs}

    def test_one_document_per_conversation(self):
        self.assertEqual(len(self.docs), 2)
        self.assertIn("Cooking pasta", self.by_title)
        self.assertIn("Weather tool call", self.by_title)

    def test_metadata_and_dates(self):
        doc = self.by_title["Cooking pasta"]
        self.assertEqual(doc.source_type, "chatgpt_conversation")
        self.assertEqual(doc.provenance, "uploaded")
        self.assertTrue(doc.doc_id.startswith("cg_"))
        # create_time 1700000000.0 -> 2023-11-14 (UTC).
        self.assertIsNotNone(doc.published_at)
        self.assertTrue(doc.published_at.startswith("2023-11-14"))

    def test_system_and_null_parts_dropped_roles_retained(self):
        doc = self.by_title["Cooking pasta"]
        # System message with empty part is dropped; user+assistant remain.
        joined = " ".join(c.text for c in doc.chunks)
        self.assertIn("user: How do I cook spaghetti al dente?", joined)
        self.assertIn("assistant: Boil salted water", joined)
        self.assertNotIn("system:", joined)
        roles = set()
        for c in doc.chunks:
            roles.update(c.meta["roles"])
            self.assertIn("message_ids", c.meta)
            # Derivable source_type and the always-empty refs:[] are no longer
            # parked in chunk meta (lead-approved schema deviation).
            self.assertNotIn("source_type", c.meta)
            self.assertNotIn("refs", c.meta)
        self.assertEqual(roles, {"user", "assistant"})

    def test_tool_kept_assistant_null_dropped(self):
        doc = self.by_title["Weather tool call"]
        joined = " ".join(c.text for c in doc.chunks)
        self.assertIn("user: What is the weather", joined)
        self.assertIn("tool:", joined)  # tool result retained
        # The final assistant message had null parts -> no assistant turn kept.
        self.assertNotIn("assistant:", joined)

    def test_deterministic_doc_ids(self):
        again = {d.title: d.doc_id for d in chatgpt.load(_FIXTURE)}
        for title, doc in self.by_title.items():
            self.assertEqual(again[title], doc.doc_id)

    def test_seq_is_contiguous_and_ordered(self):
        doc = self.by_title["Cooking pasta"]
        self.assertEqual([c.seq for c in doc.chunks], list(range(len(doc.chunks))))


class TestDocumentAdapter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="agent_os_memtest_")
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, content):
        p = self.root / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_markdown_title_and_type(self):
        p = self._write("note.md", "# My Heading\n\nSome body text about turbines.")
        docs = documents.load(p)
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].title, "My Heading")
        self.assertEqual(docs[0].source_type, "document")
        self.assertTrue(docs[0].doc_id.startswith("doc_"))
        self.assertIn("turbines", " ".join(c.text for c in docs[0].chunks))

    def test_news_type_and_published_at(self):
        p = self._write("story.txt", "Markets rallied on the news today.")
        docs = documents.load(p, source_type="news", published_at="2024-05-01")
        self.assertEqual(docs[0].source_type, "news")
        self.assertEqual(docs[0].published_at, "2024-05-01")
        # doc_id derives from the path only (never the source_type label).
        self.assertTrue(docs[0].doc_id.startswith("doc_"))

    def test_doc_id_is_path_only_regardless_of_label(self):
        # Same file loaded as document vs. news maps to the SAME document id, so
        # re-labeling updates the existing doc instead of duplicating it.
        p = self._write("same.txt", "One file, one document.")
        as_doc = documents.load(p)[0].doc_id
        as_news = documents.load(p, source_type="news", published_at="2024-01-01")[0].doc_id
        self.assertEqual(as_doc, as_news)

    def test_content_hash_changes_with_content(self):
        p1 = self._write("a.txt", "original content")
        h1 = documents.load(p1)[0].content_hash
        p1.write_text("different content", encoding="utf-8")
        h2 = documents.load(p1)[0].content_hash
        self.assertNotEqual(h1, h2)


class TestDetectAndLoad(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="agent_os_memtest_")
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_directory_mixed_files(self):
        (self.root / "a.md").write_text("# A\n\nalpha", encoding="utf-8")
        (self.root / "b.txt").write_text("beta gamma", encoding="utf-8")
        (self.root / "ignore.bin").write_text("nope", encoding="utf-8")
        docs, errors = base.detect_and_load(str(self.root))
        self.assertEqual(len(docs), 2)  # .bin skipped
        self.assertEqual(errors, [])

    def test_json_export_detected_in_directory(self):
        import shutil

        shutil.copy(_FIXTURE, self.root / "conversations.json")
        docs, errors = base.detect_and_load(str(self.root))
        self.assertEqual(len(docs), 2)
        self.assertTrue(all(d.source_type == "chatgpt_conversation" for d in docs))
        self.assertEqual(errors, [])

    def test_news_requires_published_at(self):
        (self.root / "n.txt").write_text("news body", encoding="utf-8")
        with self.assertRaises(ValueError):
            base.detect_and_load(str(self.root), source_type="news")

    def test_invalid_source_type(self):
        with self.assertRaises(ValueError):
            base.detect_and_load(str(self.root), source_type="bogus")

    def test_cannot_label_prose_as_chatgpt_conversation(self):
        # chatgpt_conversation is adapter-assigned; a caller may not force it on
        # md/txt (that would poison the source_type filter).
        (self.root / "x.txt").write_text("body", encoding="utf-8")
        with self.assertRaises(ValueError):
            base.detect_and_load(str(self.root), source_type="chatgpt_conversation")

    def test_invalid_published_at_rejected(self):
        (self.root / "n.txt").write_text("news body", encoding="utf-8")
        with self.assertRaises(ValueError):
            base.detect_and_load(str(self.root), source_type="news", published_at="7/4/2026")

    def test_missing_path(self):
        with self.assertRaises(FileNotFoundError):
            base.detect_and_load(str(self.root / "nope"))

    def test_malformed_json_in_directory_is_collected_not_fatal(self):
        # A corrupt conversations.json must not abort the whole walk; good files
        # still ingest and the bad one is reported in errors.
        (self.root / "good.txt").write_text("clean prose", encoding="utf-8")
        (self.root / "conversations.json").write_text("{ this is not valid json", encoding="utf-8")
        docs, errors = base.detect_and_load(str(self.root))
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].source_type, "document")
        self.assertEqual(len(errors), 1)
        self.assertIn("conversations.json", errors[0]["path"])

    def test_non_dict_mapping_nodes_tolerated(self):
        # A mapping whose node values are null/strings must be skipped gracefully
        # (per the chatgpt adapter docstring), not crash the ingest.
        import json

        payload = [
            {
                "title": "Broken",
                "mapping": {
                    "root": None,
                    "n0": "not-a-dict",
                    "n1": {"id": "n1", "message": {"id": "m", "author": {"role": "user"},
                            "content": {"content_type": "text", "parts": ["hello there friend"]}},
                           "parent": None, "children": []},
                },
                "current_node": "n1",
            }
        ]
        p = self.root / "conversations.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        docs, errors = base.detect_and_load(str(self.root))
        self.assertEqual(errors, [])
        self.assertEqual(len(docs), 1)
        self.assertIn("hello there friend", " ".join(c.text for c in docs[0].chunks))

    def test_single_unsupported_file_errors_clearly(self):
        # An explicitly named unsupported file returns a clear error, not a
        # success-shaped no-op.
        p = self.root / "data.bin"
        p.write_text("binary-ish", encoding="utf-8")
        with self.assertRaises(ValueError):
            base.detect_and_load(str(p))

    def test_single_malformed_json_errors(self):
        p = self.root / "conversations.json"
        p.write_text("{bad", encoding="utf-8")
        with self.assertRaises(Exception):
            base.detect_and_load(str(p))


class TestLoadContent(unittest.TestCase):
    """The content analog of detect_and_load used by the hub ingest endpoint."""

    def test_prose_with_source_path_is_deterministic_doc_id(self):
        docs = base.load_content("# Cats\n\nCats purr.", source_path="/corpus/cats.md")
        self.assertEqual(len(docs), 1)
        doc = docs[0]
        self.assertEqual(doc.source_type, "document")
        self.assertEqual(doc.provenance, "uploaded")
        self.assertEqual(doc.source_path, "/corpus/cats.md")
        self.assertEqual(doc.title, "Cats")
        self.assertEqual(doc.doc_id, base._doc_id("doc", "/corpus/cats.md"))
        # Re-deriving from the same path is stable (drives replace-on-change).
        again = base.load_content("Different text.", source_path="/corpus/cats.md")
        self.assertEqual(again[0].doc_id, doc.doc_id)

    def test_prose_without_source_path_uses_injected_minter(self):
        docs = base.load_content("Some content.", mint_doc_id=lambda: "mem_ULIDSENTINEL")
        self.assertEqual(docs[0].doc_id, "mem_ULIDSENTINEL")
        self.assertIsNone(docs[0].source_path)

    def test_prose_without_source_path_or_minter_is_content_addressed(self):
        text = "Standalone content with no identity key."
        d1 = base.load_content(text)
        d2 = base.load_content(text)
        self.assertEqual(d1[0].doc_id, d2[0].doc_id)  # deterministic fallback

    def test_news_requires_published_at(self):
        with self.assertRaises(ValueError):
            base.load_content("x", source_type="news", source_path="/n.md")
        ok = base.load_content(
            "market report", source_type="news", source_path="/n.md",
            published_at="2024-01-15",
        )
        self.assertEqual(ok[0].source_type, "news")
        self.assertEqual(ok[0].published_at, "2024-01-15")

    def test_adapter_assigned_type_is_rejected(self):
        with self.assertRaises(ValueError):
            base.load_content("x", source_type="chatgpt_conversation")

    def test_chatgpt_export_content_detected(self):
        text = _FIXTURE.read_text(encoding="utf-8")
        docs = base.load_content(text, source_path="/uploads/export.json")
        self.assertEqual(len(docs), 2)
        self.assertTrue(all(d.source_type == "chatgpt_conversation" for d in docs))
        self.assertTrue(all(d.doc_id.startswith("cg_") for d in docs))

    def test_load_export_matches_file_load(self):
        # load(path) delegates to load_export(text, path): identical documents.
        text = _FIXTURE.read_text(encoding="utf-8")
        from_file = chatgpt.load(_FIXTURE)
        from_text = chatgpt.load_export(text, str(_FIXTURE.resolve()))
        self.assertEqual([d.doc_id for d in from_file], [d.doc_id for d in from_text])
        self.assertEqual([d.title for d in from_file], [d.title for d in from_text])


if __name__ == "__main__":
    unittest.main()
