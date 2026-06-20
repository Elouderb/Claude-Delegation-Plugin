"""
Tests for scripts/hook_common.py.

Covers:
  - graphify_command() — default and environment-variable overrides
  - refresh_graphify() — lock-file behaviour (already running / success / failure)
    via a stubbed subprocess, never calling a real graphify binary.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

# Ensure scripts/ is on the path.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import hook_common  # noqa: E402


class TestGraphifyCommand(unittest.TestCase):
    """graphify_command() returns the correct command list."""

    def test_default_command(self):
        with patch.dict(os.environ, {}, clear=False):
            # Ensure overrides are absent
            os.environ.pop("AGENT_OS_GRAPHIFY_EXECUTABLE", None)
            os.environ.pop("AGENT_OS_GRAPHIFY_ARGS", None)
            cmd = hook_common.graphify_command(Path("/fake/root"))
        self.assertEqual(cmd, ["graphify", "update", ".", "--force"])

    def test_custom_executable(self):
        with patch.dict(os.environ, {"AGENT_OS_GRAPHIFY_EXECUTABLE": "my-graphify",
                                     "AGENT_OS_GRAPHIFY_ARGS": ""}, clear=False):
            # Remove ARGS to keep test isolated
            os.environ.pop("AGENT_OS_GRAPHIFY_ARGS", None)
            cmd = hook_common.graphify_command(Path("/fake/root"))
        self.assertEqual(cmd[0], "my-graphify")
        self.assertEqual(cmd[1:], ["update", ".", "--force"])

    def test_extra_args_appended(self):
        with patch.dict(os.environ, {"AGENT_OS_GRAPHIFY_ARGS": "--backend ollama --model llama3",
                                     "AGENT_OS_GRAPHIFY_EXECUTABLE": "graphify"}, clear=False):
            cmd = hook_common.graphify_command(Path("/fake/root"))
        self.assertIn("--backend", cmd)
        self.assertIn("ollama", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("llama3", cmd)
        # Base command still at the front
        self.assertEqual(cmd[:4], ["graphify", "update", ".", "--force"])

    def test_extra_args_absent_when_env_not_set(self):
        env = {"AGENT_OS_GRAPHIFY_EXECUTABLE": "graphify"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("AGENT_OS_GRAPHIFY_ARGS", None)
            cmd = hook_common.graphify_command(Path("/fake/root"))
        self.assertEqual(cmd, ["graphify", "update", ".", "--force"])


class TestRefreshGraphifyLockBehavior(unittest.TestCase):
    """refresh_graphify() lock-file tests using stubbed subprocess."""

    def _make_root(self):
        tmpdir = tempfile.mkdtemp()
        # create the .agent-os/hooks dir so log() etc. work
        (Path(tmpdir) / ".agent-os" / "hooks").mkdir(parents=True)
        return Path(tmpdir), tmpdir

    def _teardown_root(self, tmpdir):
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    def _make_completed_process(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr
        )

    # ------------------------------------------------------------------ success

    def test_success_removes_dirty_flag(self):
        root, tmpdir = self._make_root()
        try:
            # Pre-create dirty flag
            dirty = root / ".agent-os" / "hooks" / "graphify.dirty"
            dirty.write_text("{}")
            ok_result = self._make_completed_process(0, stdout="updated")
            with patch("subprocess.run", return_value=ok_result):
                with patch.object(hook_common, "git_state_fingerprint", return_value="abc123"):
                    success, msg = hook_common.refresh_graphify(root, "test-reason")
            self.assertTrue(success)
            self.assertFalse(dirty.exists())
        finally:
            self._teardown_root(tmpdir)

    def test_success_writes_fingerprint(self):
        root, tmpdir = self._make_root()
        try:
            ok_result = self._make_completed_process(0)
            with patch("subprocess.run", return_value=ok_result):
                with patch.object(hook_common, "git_state_fingerprint", return_value="deadbeef"):
                    hook_common.refresh_graphify(root, "test-reason")
            fp = root / ".agent-os" / "hooks" / "git-state.sha256"
            self.assertEqual(fp.read_text().strip(), "deadbeef")
        finally:
            self._teardown_root(tmpdir)

    def test_success_message_contains_reason(self):
        root, tmpdir = self._make_root()
        try:
            ok_result = self._make_completed_process(0)
            with patch("subprocess.run", return_value=ok_result):
                with patch.object(hook_common, "git_state_fingerprint", return_value="x"):
                    success, msg = hook_common.refresh_graphify(root, "my-reason")
            self.assertTrue(success)
            self.assertIn("my-reason", msg)
        finally:
            self._teardown_root(tmpdir)

    # ------------------------------------------------------------------ failure

    def test_nonzero_exit_returns_false(self):
        root, tmpdir = self._make_root()
        try:
            fail_result = self._make_completed_process(1, stderr="some error")
            with patch("subprocess.run", return_value=fail_result):
                success, msg = hook_common.refresh_graphify(root, "test")
            self.assertFalse(success)
            self.assertIn("some error", msg)
        finally:
            self._teardown_root(tmpdir)

    def test_executable_not_found(self):
        root, tmpdir = self._make_root()
        try:
            with patch("subprocess.run", side_effect=FileNotFoundError):
                success, msg = hook_common.refresh_graphify(root, "test")
            self.assertFalse(success)
            self.assertIn("not found", msg.lower())
        finally:
            self._teardown_root(tmpdir)

    def test_timeout_returns_false(self):
        root, tmpdir = self._make_root()
        try:
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=[], timeout=160)):
                success, msg = hook_common.refresh_graphify(root, "test", timeout=160)
            self.assertFalse(success)
            self.assertIn("timed out", msg.lower())
        finally:
            self._teardown_root(tmpdir)

    def test_no_llm_key_treated_as_success(self):
        """'no llm api key' in stderr should be treated as a clean no-op (True)."""
        root, tmpdir = self._make_root()
        try:
            fail_result = self._make_completed_process(1, stderr="Error: no LLM API key configured")
            with patch("subprocess.run", return_value=fail_result):
                with patch.object(hook_common, "git_state_fingerprint", return_value="x"):
                    success, msg = hook_common.refresh_graphify(root, "test")
            self.assertTrue(success)
            self.assertIn("no LLM API key", msg)
        finally:
            self._teardown_root(tmpdir)

    # ------------------------------------------------------------------ lock

    def test_existing_fresh_lock_skips_run(self):
        """If a fresh lock file exists, refresh_graphify returns True immediately."""
        root, tmpdir = self._make_root()
        try:
            lock = root / ".agent-os" / "hooks" / "graphify.lock"
            lock.write_text("running")
            # Don't touch mtime so it's considered fresh (within timeout)
            mock_run = MagicMock()
            with patch("subprocess.run", mock_run):
                success, msg = hook_common.refresh_graphify(root, "test", timeout=160)
            mock_run.assert_not_called()
            self.assertTrue(success)
            self.assertIn("already running", msg)
        finally:
            self._teardown_root(tmpdir)

    def test_stale_lock_is_removed_and_run_proceeds(self):
        """A lock file older than timeout+30 s is removed and the run proceeds."""
        root, tmpdir = self._make_root()
        try:
            lock = root / ".agent-os" / "hooks" / "graphify.lock"
            lock.write_text("stale")
            # Set mtime to well in the past (200 s ago for timeout=160 → threshold=190 s)
            past = time.time() - 200
            os.utime(str(lock), (past, past))

            ok_result = self._make_completed_process(0)
            with patch("subprocess.run", return_value=ok_result):
                with patch.object(hook_common, "git_state_fingerprint", return_value="x"):
                    success, msg = hook_common.refresh_graphify(root, "test", timeout=160)
            self.assertTrue(success)
            # Lock should be cleaned up after the run
            self.assertFalse(lock.exists())
        finally:
            self._teardown_root(tmpdir)

    def test_lock_cleaned_up_after_success(self):
        root, tmpdir = self._make_root()
        try:
            ok_result = self._make_completed_process(0)
            with patch("subprocess.run", return_value=ok_result):
                with patch.object(hook_common, "git_state_fingerprint", return_value="x"):
                    hook_common.refresh_graphify(root, "test")
            lock = root / ".agent-os" / "hooks" / "graphify.lock"
            self.assertFalse(lock.exists())
        finally:
            self._teardown_root(tmpdir)

    def test_lock_cleaned_up_after_failure(self):
        root, tmpdir = self._make_root()
        try:
            fail_result = self._make_completed_process(1, stderr="err")
            with patch("subprocess.run", return_value=fail_result):
                hook_common.refresh_graphify(root, "test")
            lock = root / ".agent-os" / "hooks" / "graphify.lock"
            self.assertFalse(lock.exists())
        finally:
            self._teardown_root(tmpdir)


if __name__ == "__main__":
    unittest.main()
