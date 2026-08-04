"""Regression tests for the emit-safety invariant (card ce79a987 finding 1).

The invariant: event emission must NEVER break the card / graph / hook operation
it hangs off, even on a checkout where ``contracts`` (or its pydantic dependency)
cannot be imported. The original defect was that each typed emitter did
``from contracts import EventType`` *outside* ``emit_event``'s swallow and before
the kill-switch, so a broken ``contracts`` raised out of the emitter and (a)
turned an already-committed card into a reported failure, (b) turned a successful
graph refresh into a reported failure, and (c) could crash the hook subprocess.

These tests reproduce the reviewer's ``sys.modules['contracts'] = None`` poisoning
and assert each path now degrades to a silent emission no-op while the underlying
operation succeeds.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MCP_DIR = _REPO_ROOT / "mcp"
_SYNC_SCRIPT = _REPO_ROOT / "scripts" / "sync_repo_graph.py"
for _p in (str(_REPO_ROOT), str(_MCP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import card_tools  # noqa: E402
import graph_io  # noqa: E402
import shared_graph_tools  # noqa: E402
from outbox import emit, identity, paths as outbox_paths  # noqa: E402


class _PoisonContracts:
    """Context manager that makes ``import contracts`` raise (reviewer's repro)."""

    def __enter__(self):
        self._present = "contracts" in sys.modules
        self._saved = sys.modules.get("contracts")
        sys.modules["contracts"] = None  # type: ignore[assignment]
        return self

    def __exit__(self, *exc):
        if self._present:
            sys.modules["contracts"] = self._saved  # type: ignore[assignment]
        else:
            sys.modules.pop("contracts", None)
        return False


class _InProcessBase(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory(prefix="agent_os_home_")
        self._repo = tempfile.TemporaryDirectory(prefix="agent_os_repo_")
        self._prev_home = os.environ.get("AGENT_OS_HOME")
        os.environ["AGENT_OS_HOME"] = self._home.name
        self.repo = Path(self._repo.name)
        (self.repo / ".agent-os").mkdir(parents=True, exist_ok=True)

        # Wire the card DB + emission root exactly as server.ensure_agent_os does.
        card_tools.set_db_path(self.repo / ".agent-os" / "cards.sqlite")
        card_tools.init_db()
        graph_io.set_emission_repo_root(self.repo)
        graph_io._reset_outbox_cache_for_tests()
        emit._reset_ids_cache_for_tests()
        # Bootstrap identity + prime the outbox import while contracts is healthy.
        identity.bootstrap_identity(self.repo)
        self.assertIsNotNone(graph_io.get_outbox())

    def tearDown(self):
        card_tools.set_db_path(None)
        graph_io.set_emission_repo_root(None)
        graph_io._reset_outbox_cache_for_tests()
        emit._reset_ids_cache_for_tests()
        if self._prev_home is None:
            os.environ.pop("AGENT_OS_HOME", None)
        else:
            os.environ["AGENT_OS_HOME"] = self._prev_home
        self._home.cleanup()
        self._repo.cleanup()

    def _outbox_line_count(self) -> int:
        path = outbox_paths.outbox_path(self.repo)
        if not path.exists():
            return 0
        return len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln])


class TestCreateCardSurvivesBrokenContracts(_InProcessBase):
    def test_create_card_returns_card_id_when_contracts_unimportable(self):
        before = self._outbox_line_count()
        with _PoisonContracts():
            result = card_tools.create_card("Broken-contracts card", priority="high")
        # The card was written AND returned — not reported as a failure.
        self.assertNotIn("error", result)
        self.assertIn("card_id", result)
        self.assertEqual(result["title"], "Broken-contracts card")
        # The card really is in the DB.
        fetched = card_tools.get_card(result["card_id"])
        self.assertEqual(fetched["card_id"], result["card_id"])
        # Emission no-oped: no new outbox line was written.
        self.assertEqual(self._outbox_line_count(), before)

    def test_update_and_complete_survive_broken_contracts(self):
        created = card_tools.create_card("c")  # healthy emit here
        with _PoisonContracts():
            updated = card_tools.update_card(created["card_id"], status="In Progress")
            self.assertNotIn("error", updated)
            self.assertEqual(updated["status"], "In Progress")
            completed = card_tools.complete_card(created["card_id"], "done")
            self.assertNotIn("error", completed)
            self.assertEqual(completed["status"], "Complete")


class TestGraphRefreshSurvivesBrokenContracts(_InProcessBase):
    def test_db_graph_refresh_reports_refreshed_when_contracts_unimportable(self):
        # Stub the actual refresh so we exercise the success + emission path
        # without a live DB; the emission is what must not turn success into
        # failure.
        original = shared_graph_tools.refresh_database_graph
        shared_graph_tools.refresh_database_graph = lambda: (True, None)
        try:
            with _PoisonContracts():
                result = shared_graph_tools.graph_refresh("database")
        finally:
            shared_graph_tools.refresh_database_graph = original
        self.assertEqual(result["results"], {"status": "refreshed"})
        self.assertEqual(result["warnings"], [])


class TestHookLifecycleSurvivesBrokenContracts(unittest.TestCase):
    """The hook subprocess must exit 0 (and emit nothing) even when contracts is
    unimportable in the child."""

    def setUp(self):
        import shutil

        self.git = shutil.which("git")
        if self.git is None:
            self.skipTest("git not available")
        self._home = tempfile.TemporaryDirectory(prefix="agent_os_home_")
        self._repo = tempfile.TemporaryDirectory(prefix="agent_os_hookrepo_")
        self._poison = tempfile.TemporaryDirectory(prefix="agent_os_poison_")
        self.home = Path(self._home.name)
        self.repo = Path(self._repo.name)
        subprocess.run([self.git, "-C", str(self.repo), "init", "-q"], check=True)
        (self.repo / "graphify-out").mkdir(parents=True)
        (self.repo / "graphify-out" / "GRAPH_REPORT.md").write_text("ok", encoding="utf-8")
        # A sitecustomize that poisons `contracts` at interpreter startup, before
        # the hook imports anything (mirrors the reviewer's repro, cross-process).
        Path(self._poison.name, "sitecustomize.py").write_text(
            "import sys\nsys.modules['contracts'] = None\n", encoding="utf-8"
        )

    def tearDown(self):
        self._home.cleanup()
        self._repo.cleanup()
        self._poison.cleanup()

    def test_session_start_hook_exits_zero_and_emits_nothing(self):
        env = dict(os.environ)
        env["AGENT_OS_HOME"] = str(self.home)
        env["PYTHONPATH"] = os.pathsep.join(
            [self._poison.name, env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        payload = {"cwd": str(self.repo), "session_id": "sess_x", "source": "startup"}
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
        # Emission no-oped: no outbox file (or no agent events) was produced.
        outbox = self.repo / ".agent-os" / "sync" / "outbox.jsonl"
        if outbox.exists():
            self.assertNotIn("agent.", outbox.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
