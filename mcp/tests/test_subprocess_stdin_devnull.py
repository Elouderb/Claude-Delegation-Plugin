"""stdin=subprocess.DEVNULL regression guard (card 2f94ecee, comment 166 MAJOR-1).

The Windows companion-hang root cause (comment 162) was a spawned child inheriting
the MCP server's blocking-read stdin pipe: on Windows the child's interpreter
startup queues behind the parent's pending pipe read and hangs before running any
Python. ``capture_output=True`` sets only stdout/stderr, so every subprocess spawn
reachable from the MCP-server process — and, defensively, the hook processes —
must pass ``stdin=subprocess.DEVNULL`` so no child can inherit that pipe.

These tests mock ``subprocess.run`` and assert the flag is passed. They are the
cross-platform proof for a bug that only manifests on Windows (the box these run
on is Linux), locking in the fix so a future edit cannot silently drop it.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_MCP_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _MCP_DIR.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _p in (str(_MCP_DIR), str(_REPO_ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import shared_graph_tools  # noqa: E402
import hook_common  # type: ignore[import]  # noqa: E402  (scripts/ resolved at runtime)
from outbox import identity  # noqa: E402


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr=stderr)


class TestMcpServerProcessSpawns(unittest.TestCase):
    """The two subprocess spawns that execute IN the MCP-server process."""

    def test_graph_refresh_code_passes_stdin_devnull(self):
        # The graphify console-script spawn — the exact site from MAJOR-1.
        with patch("subprocess.run", return_value=_completed(0, stdout="")) as m_run, \
                patch.object(shared_graph_tools, "_emit_graph_event"):
            shared_graph_tools.graph_refresh("code")
        self.assertTrue(m_run.called)
        args, kwargs = m_run.call_args
        self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertIn("update", args[0])  # sanity: it was the graphify update spawn

    def test_git_remote_origin_passes_stdin_devnull(self):
        # Runs at server startup via outbox.bootstrap_identity -> here, BEFORE
        # server.run(); a hang would take every tool down.
        with patch("subprocess.run",
                   return_value=_completed(0, stdout="git@example.com:o/r.git\n")) as m_run:
            identity.git_remote_origin(Path(tempfile.gettempdir()))
        self.assertTrue(m_run.called)
        _, kwargs = m_run.call_args
        self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)


class TestHookProcessSpawns(unittest.TestCase):
    """Defensive: the hook-process spawns (separate processes, same Windows class)."""

    def test_git_root_passes_stdin_devnull(self):
        with patch("subprocess.run",
                   return_value=_completed(0, stdout=str(Path.cwd()) + "\n")) as m_run:
            hook_common.git_root(Path.cwd())
        self.assertTrue(m_run.called)
        _, kwargs = m_run.call_args
        self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)

    def test_git_state_fingerprint_passes_stdin_devnull(self):
        # git_state_fingerprint hashes result.stdout as BYTES and runs two commands.
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"")
        with patch("subprocess.run", return_value=result) as m_run:
            hook_common.git_state_fingerprint(Path.cwd())
        self.assertTrue(m_run.called)
        for call_obj in m_run.call_args_list:
            _, kwargs = call_obj
            self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)

    def test_refresh_graphify_passes_stdin_devnull(self):
        # The graphify console-script spawn in the HOOK path — identical hang class.
        tmpdir = tempfile.mkdtemp(prefix="agent_os_stdintest_")
        self.addCleanup(shutil.rmtree, tmpdir, True)
        root = Path(tmpdir)
        (root / ".agent-os" / "hooks").mkdir(parents=True)
        with patch("subprocess.run", return_value=_completed(0, stdout="updated")) as m_run, \
                patch.object(hook_common, "git_state_fingerprint", return_value="abc"):
            hook_common.refresh_graphify(root, "stdin-test")
        self.assertTrue(m_run.called)
        _, kwargs = m_run.call_args  # refresh_graphify's only spawn is graphify
        self.assertIs(kwargs.get("stdin"), subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
