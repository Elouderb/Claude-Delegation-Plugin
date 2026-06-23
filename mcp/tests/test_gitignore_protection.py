"""
Tests for the .agent-os/.gitignore self-protection helpers.

Verifies:
  - The helper creates .agent-os/.gitignore with wildcard content on a fresh
    temp directory.
  - The helper is idempotent: it does not clobber an existing file with
    different content.
  - ensure_agent_os() (server path) triggers the helper.
  - state_dir() (hook_common path) triggers the helper.

All tests use temporary directories; none touch the real repo's .agent-os/.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# ── server.py lives at mcp/ root ──────────────────────────────────────────────
_MCP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_MCP_DIR))

# ── hook_common.py lives at scripts/ ─────────────────────────────────────────
_SCRIPTS_DIR = _MCP_DIR.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))


class TestServerGitignoreHelper(unittest.TestCase):
    """Tests for server._ensure_agent_os_gitignore()."""

    def _import_helper(self):
        import server as _server
        return _server._ensure_agent_os_gitignore

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.agent_os_dir = Path(self._tmp.name) / ".agent-os"
        self.agent_os_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_gitignore_with_wildcard(self):
        helper = self._import_helper()
        helper(self.agent_os_dir)
        gi = self.agent_os_dir / ".gitignore"
        self.assertTrue(gi.exists(), ".gitignore should be created")
        self.assertEqual(gi.read_text(encoding="utf-8"), "*\n")

    def test_idempotent_does_not_clobber_existing(self):
        helper = self._import_helper()
        gi = self.agent_os_dir / ".gitignore"
        existing_content = "# custom user ignore\ncards.sqlite\n"
        gi.write_text(existing_content, encoding="utf-8")
        helper(self.agent_os_dir)
        self.assertEqual(
            gi.read_text(encoding="utf-8"),
            existing_content,
            "Existing .gitignore must not be overwritten",
        )

    def test_oserror_is_swallowed(self):
        """An OSError during write must not propagate."""
        import server as _server
        helper = _server._ensure_agent_os_gitignore
        # Pass a path that doesn't exist as a directory so write_text would
        # succeed normally, but patch write_text to raise OSError.
        gi = self.agent_os_dir / ".gitignore"
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            # Should not raise
            try:
                helper(self.agent_os_dir)
            except OSError:
                self.fail("_ensure_agent_os_gitignore must swallow OSError")


class TestHookCommonGitignoreHelper(unittest.TestCase):
    """Tests for hook_common._ensure_agent_os_gitignore()."""

    def _import_helper(self):
        import hook_common
        return hook_common._ensure_agent_os_gitignore

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.agent_os_dir = Path(self._tmp.name) / ".agent-os"
        self.agent_os_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_creates_gitignore_with_wildcard(self):
        helper = self._import_helper()
        helper(self.agent_os_dir)
        gi = self.agent_os_dir / ".gitignore"
        self.assertTrue(gi.exists(), ".gitignore should be created")
        self.assertEqual(gi.read_text(encoding="utf-8"), "*\n")

    def test_idempotent_does_not_clobber_existing(self):
        helper = self._import_helper()
        gi = self.agent_os_dir / ".gitignore"
        existing_content = "# custom\nhooks/\n"
        gi.write_text(existing_content, encoding="utf-8")
        helper(self.agent_os_dir)
        self.assertEqual(
            gi.read_text(encoding="utf-8"),
            existing_content,
            "Existing .gitignore must not be overwritten",
        )

    def test_oserror_is_swallowed(self):
        """An OSError during write must not propagate."""
        import hook_common
        helper = hook_common._ensure_agent_os_gitignore
        with patch.object(Path, "write_text", side_effect=OSError("no space")):
            try:
                helper(self.agent_os_dir)
            except OSError:
                self.fail("_ensure_agent_os_gitignore must swallow OSError")


class TestEnsureAgentOsCallsGitignore(unittest.TestCase):
    """ensure_agent_os() in server.py must invoke _ensure_agent_os_gitignore."""

    def test_gitignore_created_by_ensure_agent_os(self):
        import server as _server
        import card_tools

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            # Create a fake .git so ensure_agent_os finds this as repo root
            (root / ".git").mkdir()

            # Patch get_repo_root is not used in ensure_agent_os directly;
            # it uses Path.cwd() — so we patch cwd instead.
            with patch.object(Path, "cwd", return_value=root):
                # Prevent graph server from starting in tests
                with patch.object(_server, "start_graph_server"):
                    # Prevent card_tools from touching a real DB path
                    with patch.object(card_tools, "set_db_path"):
                        with patch.object(card_tools, "init_db"):
                            _server.ensure_agent_os()

            gi = root / ".agent-os" / ".gitignore"
            self.assertTrue(gi.exists(), "ensure_agent_os must create .agent-os/.gitignore")
            self.assertEqual(gi.read_text(encoding="utf-8"), "*\n")


class TestStateDirCallsGitignore(unittest.TestCase):
    """state_dir() in hook_common.py must invoke _ensure_agent_os_gitignore."""

    def test_gitignore_created_by_state_dir(self):
        import hook_common

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            hook_common.state_dir(root)
            gi = root / ".agent-os" / ".gitignore"
            self.assertTrue(gi.exists(), "state_dir must create .agent-os/.gitignore")
            self.assertEqual(gi.read_text(encoding="utf-8"), "*\n")


if __name__ == "__main__":
    unittest.main()
