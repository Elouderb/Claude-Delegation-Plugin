"""
Regression tests for card-database resilience to the cards.sqlite file being
replaced underneath the running server (SQLITE_READONLY_DBMOVED).

A long-lived connection keeps pointing at the original inode; once the file is
replaced (git operation touching the directory, external rewrite, delete +
recreate), SQLite flips that stale handle to read-only and every write fails.
card_tools now opens a fresh connection per operation and re-ensures the schema
on each connect, so operations must keep succeeding after an inode swap.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# card_tools lives at the mcp/ root (one level up from this tests/ package).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import card_tools  # noqa: E402


class TestCardDbResilience(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "cards.sqlite"
        card_tools.set_db_path(self.db_path)
        card_tools.init_db()

    def tearDown(self):
        card_tools.set_db_path(None)
        self._tmp.cleanup()

    def _swap_inode(self):
        """Atomically replace cards.sqlite with a different, freshly-created DB.

        os.replace() gives the path a new inode, exactly as a git checkout or an
        external rewrite would. The replacement starts schema-less to also prove
        the per-connection schema self-heal.
        """
        replacement = Path(self._tmp.name) / "replacement.sqlite"
        sqlite3.connect(str(replacement)).close()  # empty file, no tables
        os.replace(str(replacement), str(self.db_path))

    def test_create_card_survives_inode_swap(self):
        first = card_tools.create_card("before swap")
        self.assertNotIn("error", first, f"baseline create failed: {first}")

        self._swap_inode()

        # A stale held connection would be read-only here; per-op must succeed.
        second = card_tools.create_card("after swap")
        self.assertNotIn("error", second, f"create_card failed after inode swap: {second}")

        got = card_tools.get_card(second["card_id"])
        self.assertEqual(got.get("title"), "after swap")

    def test_add_comment_survives_inode_swap(self):
        card = card_tools.create_card("commentable")
        self.assertNotIn("error", card)

        self._swap_inode()

        # Card table was re-created empty by the swap, so add a fresh card to
        # comment on, then confirm the write + read both work post-swap.
        card2 = card_tools.create_card("post-swap card")
        self.assertNotIn("error", card2)
        comment = card_tools.add_comment(card2["card_id"], "tester", "still writable")
        self.assertNotIn("error", comment, f"add_comment failed after inode swap: {comment}")

        got = card_tools.get_card(card2["card_id"])
        self.assertEqual(len(got.get("comments", [])), 1)
        self.assertEqual(got["comments"][0]["comment"], "still writable")

    def test_complete_card_survives_inode_swap(self):
        self._swap_inode()
        card = card_tools.create_card("to complete")
        self.assertNotIn("error", card)
        result = card_tools.complete_card(card["card_id"], "done post-swap")
        self.assertEqual(result.get("status"), "Complete")
        # Completion summary persisted as a comment.
        comments = result.get("comments", [])
        self.assertTrue(any("done post-swap" in (c.get("comment") or "") for c in comments))

    def test_uninitialized_database_returns_error(self):
        card_tools.set_db_path(None)
        result = card_tools.create_card("no db")
        self.assertIn("error", result)
        self.assertIn("not initialized", result["error"].lower())


if __name__ == "__main__":
    unittest.main()
