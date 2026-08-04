"""Hook-path event emission smoke test (scripts/sync_repo_graph.py) — Phase 1b.

Drives the REAL hook entry point as a subprocess (exactly how Claude Code invokes
it) with SessionStart / SubagentStop / SessionEnd payloads and asserts the
corresponding agent lifecycle events land in the repo-local outbox, with the
session context from the hook payload. This exercises the full scripts/ path:
``hook_common``'s repo-root sys.path insert, ``bootstrap_identity``, and
``emit_lifecycle_event``.

Hermetic: a temporary git repo (so ``git_root`` resolves) with a pre-existing
``graphify-out/GRAPH_REPORT.md`` so the graph refresh is a no-op and no real
``graphify`` binary is invoked, and ``AGENT_OS_HOME`` pointed at a temp dir so the
real machine identity is untouched. Emission runs before the refresh branch, so
it is independent of graphify either way.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SYNC_SCRIPT = _REPO_ROOT / "scripts" / "sync_repo_graph.py"
sys.path.insert(0, str(_REPO_ROOT))

from contracts import EventEnvelope  # noqa: E402
from outbox import store  # noqa: E402


def _env(rec) -> EventEnvelope:
    """Narrow a PendingEvent to its (asserted-present) envelope."""
    assert rec.envelope is not None, rec.error
    return rec.envelope


def _which_git():
    return shutil.which("git")


@unittest.skipIf(_which_git() is None, "git not available")
class TestHookLifecycleEmission(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory(prefix="agent_os_home_")
        self._repo = tempfile.TemporaryDirectory(prefix="agent_os_hookrepo_")
        self.home = Path(self._home.name)
        self.repo = Path(self._repo.name)
        git = _which_git()
        assert git is not None  # class is skipped when git is absent
        subprocess.run([git, "-C", str(self.repo), "init", "-q"], check=True)
        # Pre-create the graph report so the refresh branch is a clean no-op.
        (self.repo / "graphify-out").mkdir(parents=True)
        (self.repo / "graphify-out" / "GRAPH_REPORT.md").write_text("ok", encoding="utf-8")

    def tearDown(self):
        self._home.cleanup()
        self._repo.cleanup()

    def _run_hook(self, reason: str, payload_extra: dict, *args: str) -> None:
        payload = {"cwd": str(self.repo)}
        payload.update(payload_extra)
        env = dict(os.environ)
        env["AGENT_OS_HOME"] = str(self.home)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        result = subprocess.run(
            [sys.executable, str(_SYNC_SCRIPT), "--reason", reason, *args],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def _agent_events(self):
        pending = store.read_pending(self.repo)
        self.assertTrue(all(p.error is None for p in pending), [p.error for p in pending])
        envs = [_env(p) for p in pending]
        return [e for e in envs if e.event_type.startswith("agent.")]

    def test_session_start_emits_agent_started(self):
        self._run_hook(
            "session-start",
            {"session_id": "sess_hook1", "source": "startup"},
            "--check-git",
        )
        events = self._agent_events()
        self.assertEqual(len(events), 1)
        env = events[0]
        self.assertEqual(env.event_type, "agent.started")
        self.assertEqual(env.session_id, "sess_hook1")
        self.assertEqual(env.payload.get("source"), "startup")
        # Identity was bootstrapped by the hook entry.
        self.assertTrue(env.machine_id.startswith("mach_"))
        self.assertTrue((env.repository_id or "").startswith("repo_"))

    def test_subagent_stop_emits_agent_finished(self):
        self._run_hook(
            "subagent-stop",
            {"session_id": "sess_hook1"},
            "--check-git",
            "--if-dirty",
        )
        events = self._agent_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "agent.finished")
        self.assertEqual(events[0].payload.get("scope"), "subagent")

    def test_session_end_emits_agent_finished(self):
        self._run_hook(
            "session-end",
            {"session_id": "sess_hook1"},
            "--check-git",
            "--if-dirty",
        )
        events = self._agent_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "agent.finished")
        self.assertEqual(events[0].payload.get("scope"), "session")

    def test_non_lifecycle_reason_emits_nothing(self):
        # A graph-sync reason (fires on every prompt/tool batch) must NOT emit an
        # agent event — the hot path stays quiet.
        self._run_hook(
            "prompt-submit",
            {"session_id": "sess_hook1"},
            "--check-git",
            "--if-dirty",
        )
        self.assertEqual(self._agent_events(), [])

    def test_kill_switch_suppresses_hook_emission(self):
        env_disabled = {"AGENT_OS_EVENTS_DISABLED": "1"}
        payload = {"cwd": str(self.repo), "session_id": "sess_hook1", "source": "startup"}
        env = dict(os.environ)
        env["AGENT_OS_HOME"] = str(self.home)
        env.update(env_disabled)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        result = subprocess.run(
            [sys.executable, str(_SYNC_SCRIPT), "--reason", "session-start", "--check-git"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._agent_events(), [])


if __name__ == "__main__":
    unittest.main()
