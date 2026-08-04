"""Tests for event emission + identity bootstrap (outbox.emit / outbox.identity).

Proves every emission site produces an envelope that validates strictly against
``contracts.EventEnvelope`` (no stray keys, correct type/context/payload), that
the ``AGENT_OS_EVENTS_DISABLED`` kill-switch fully disables emission, that a
missing identity makes emission a silent no-op that creates no files, and that a
no-git / no-remote checkout is tolerated.

Every test overrides ``AGENT_OS_HOME`` to a temp dir so the real
``~/.agent-os`` is never touched (mirrors the contracts.identity test posture).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from contracts import EventEnvelope, EventType, strict_validate  # noqa: E402
from outbox import emit, identity, paths as outbox_paths, store  # noqa: E402

# The set of registered event-type values, so tests assert producers stay within
# the taxonomy.
_REGISTRY = {member.value for member in EventType}


def _env(rec) -> EventEnvelope:
    """Narrow a PendingEvent to its (asserted-present) envelope."""
    assert rec.envelope is not None, rec.error
    return rec.envelope


class _EmitBase(unittest.TestCase):
    def setUp(self):
        self._home = tempfile.TemporaryDirectory(prefix="agent_os_home_")
        self._repo = tempfile.TemporaryDirectory(prefix="agent_os_repo_")
        self._prev_home = os.environ.get("AGENT_OS_HOME")
        self._prev_disabled = os.environ.get("AGENT_OS_EVENTS_DISABLED")
        os.environ["AGENT_OS_HOME"] = self._home.name
        os.environ.pop("AGENT_OS_EVENTS_DISABLED", None)
        self.repo = Path(self._repo.name)
        emit._reset_ids_cache_for_tests()

    def tearDown(self):
        emit._reset_ids_cache_for_tests()
        for key, prev in (
            ("AGENT_OS_HOME", self._prev_home),
            ("AGENT_OS_EVENTS_DISABLED", self._prev_disabled),
        ):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        self._home.cleanup()
        self._repo.cleanup()

    def _read_envelopes(self):
        pending = store.read_pending(self.repo)
        # No malformed lines from the emitter, ever.
        self.assertTrue(all(p.error is None for p in pending), [p.error for p in pending])
        return [_env(p) for p in pending]


class TestEverySiteValidates(_EmitBase):
    def setUp(self):
        super().setUp()
        identity.bootstrap_identity(self.repo)

    def _assert_valid(self, envelope, expected_type):
        # Type is a registered taxonomy value.
        self.assertEqual(envelope.event_type, expected_type)
        self.assertIn(envelope.event_type, _REGISTRY)
        # Required context always present.
        self.assertTrue(envelope.event_id.startswith("evt_"))
        self.assertTrue(envelope.machine_id.startswith("mach_"))
        self.assertTrue(envelope.repository_id.startswith("repo_"))
        # Strictly valid — no keys beyond the declared contract.
        strict_validate(EventEnvelope, envelope.model_dump(mode="json"))

    def test_task_created(self):
        emit.emit_task_created(
            self.repo, card_id="ab12cd34", title="Impl auth", status="Created", priority="high"
        )
        env = self._read_envelopes()[0]
        self._assert_valid(env, EventType.TASK_CREATED.value)
        self.assertEqual(env.task_id, "ab12cd34")
        self.assertEqual(
            env.payload,
            {"card_id": "ab12cd34", "title": "Impl auth", "status": "Created", "priority": "high"},
        )

    def test_task_updated(self):
        emit.emit_task_updated(
            self.repo, card_id="ab12cd34", title="Impl auth", status="In Progress", priority="high"
        )
        env = self._read_envelopes()[0]
        self._assert_valid(env, EventType.TASK_UPDATED.value)
        self.assertEqual(env.task_id, "ab12cd34")
        self.assertEqual(env.payload["status"], "In Progress")

    def test_task_completed(self):
        emit.emit_task_completed(
            self.repo, card_id="ab12cd34", title="Impl auth", status="Complete", priority="high"
        )
        env = self._read_envelopes()[0]
        self._assert_valid(env, EventType.TASK_COMPLETED.value)
        self.assertEqual(env.task_id, "ab12cd34")

    def test_repo_graph_updated(self):
        emit.emit_repo_graph_updated(self.repo)
        env = self._read_envelopes()[0]
        self._assert_valid(env, EventType.REPO_GRAPH_UPDATED.value)
        self.assertEqual(env.payload, {"graph": "code", "status": "refreshed"})
        self.assertIsNone(env.task_id)

    def test_db_graph_updated(self):
        emit.emit_db_graph_updated(self.repo)
        env = self._read_envelopes()[0]
        self._assert_valid(env, EventType.DB_GRAPH_UPDATED.value)
        self.assertEqual(env.payload, {"graph": "database", "status": "refreshed"})

    def test_agent_started(self):
        emit.emit_agent_started(
            self.repo, session_id="sess_abc", source="startup", reason="session-start"
        )
        env = self._read_envelopes()[0]
        self._assert_valid(env, EventType.AGENT_STARTED.value)
        self.assertEqual(env.session_id, "sess_abc")
        self.assertEqual(env.payload, {"source": "startup", "reason": "session-start"})

    def test_agent_finished(self):
        emit.emit_agent_finished(
            self.repo, session_id="sess_abc", reason="subagent-stop", scope="subagent"
        )
        env = self._read_envelopes()[0]
        self._assert_valid(env, EventType.AGENT_FINISHED.value)
        self.assertEqual(env.session_id, "sess_abc")
        self.assertEqual(env.payload, {"reason": "subagent-stop", "scope": "subagent"})
        self.assertIsNone(env.agent_id)  # subagent type not available at the hook

    def test_all_sites_together_are_all_valid(self):
        emit.emit_task_created(self.repo, card_id="c", title="t", status="Created", priority="low")
        emit.emit_task_updated(self.repo, card_id="c", title="t", status="In Progress", priority="low")
        emit.emit_task_completed(self.repo, card_id="c", title="t", status="Complete", priority="low")
        emit.emit_repo_graph_updated(self.repo)
        emit.emit_db_graph_updated(self.repo)
        emit.emit_agent_started(self.repo, session_id="s", source="resume")
        emit.emit_agent_finished(self.repo, session_id="s", scope="session")
        envs = self._read_envelopes()
        self.assertEqual(len(envs), 7)
        for env in envs:
            self.assertIn(env.event_type, _REGISTRY)
            strict_validate(EventEnvelope, env.model_dump(mode="json"))


class TestKillSwitch(_EmitBase):
    def test_disabled_env_suppresses_emission(self):
        identity.bootstrap_identity(self.repo)
        os.environ["AGENT_OS_EVENTS_DISABLED"] = "1"
        result = emit.emit_task_created(
            self.repo, card_id="c", title="t", status="Created", priority="low"
        )
        self.assertIsNone(result)
        # Nothing written — the outbox file is never even created.
        self.assertFalse(outbox_paths.outbox_path(self.repo).exists())

    def test_reenabled_after_switch_cleared(self):
        identity.bootstrap_identity(self.repo)
        os.environ["AGENT_OS_EVENTS_DISABLED"] = "1"
        emit.emit_task_created(self.repo, card_id="c", title="t", status="Created", priority="low")
        os.environ.pop("AGENT_OS_EVENTS_DISABLED", None)
        eid = emit.emit_task_created(self.repo, card_id="c", title="t", status="Created", priority="low")
        self.assertIsNotNone(eid)
        self.assertEqual(len(self._read_envelopes()), 1)


class TestIdentityMissingNoOp(_EmitBase):
    def test_emit_without_bootstrap_is_silent_noop(self):
        # No bootstrap_identity() call → no identity files → emission no-ops and
        # creates NOTHING under .agent-os/sync (not even a log or the sync dir).
        result = emit.emit_task_created(
            self.repo, card_id="c", title="t", status="Created", priority="low"
        )
        self.assertIsNone(result)
        self.assertFalse(outbox_paths.sync_dir(self.repo).exists())


class TestNoGitTolerance(_EmitBase):
    def test_no_git_repo_bootstraps_with_null_remote(self):
        # self.repo is a plain temp dir, not a git repo.
        created = identity.bootstrap_identity(self.repo)
        assert created is not None  # first run returns the created models
        machine, repo = created
        self.assertTrue(machine.machine_id.startswith("mach_"))
        self.assertTrue(repo.repository_id.startswith("repo_"))
        self.assertIsNone(repo.canonical_remote)
        # Emission still works with a null-remote identity.
        emit.emit_task_created(self.repo, card_id="c", title="t", status="Created", priority="low")
        self.assertEqual(len(self._read_envelopes()), 1)

    def test_git_remote_origin_none_when_absent(self):
        self.assertIsNone(identity.git_remote_origin(self.repo))

    def test_git_remote_origin_reads_configured_remote(self):
        git = _which_git()
        if git is None:
            self.skipTest("git not available")
        subprocess.run([git, "-C", str(self.repo), "init", "-q"], check=True)
        subprocess.run(
            [git, "-C", str(self.repo), "remote", "add", "origin",
             "git@github.com:Acme/Widgets.git"],
            check=True,
        )
        # bootstrap stamps the NORMALIZED canonical remote on first creation.
        created = identity.bootstrap_identity(self.repo)
        assert created is not None  # first run returns the created models
        _, repo = created
        self.assertEqual(repo.canonical_remote, "github.com/acme/widgets")


def _which_git():
    import shutil

    return shutil.which("git")


class TestEventTypeConstantsMatchRegistry(unittest.TestCase):
    """The dotted-string constants the typed emitters pass to emit_event MUST
    stay in lockstep with contracts.EventType (they exist so the emitters need
    no `from contracts import EventType`, which would run outside emit_event's
    swallow and before the kill-switch — card ce79a987 finding 1)."""

    def test_constants_equal_registry_values(self):
        pairs = {
            emit.TASK_CREATED: EventType.TASK_CREATED,
            emit.TASK_UPDATED: EventType.TASK_UPDATED,
            emit.TASK_COMPLETED: EventType.TASK_COMPLETED,
            emit.REPO_GRAPH_UPDATED: EventType.REPO_GRAPH_UPDATED,
            emit.DB_GRAPH_UPDATED: EventType.DB_GRAPH_UPDATED,
            emit.AGENT_STARTED: EventType.AGENT_STARTED,
            emit.AGENT_FINISHED: EventType.AGENT_FINISHED,
        }
        for constant, member in pairs.items():
            self.assertEqual(constant, member.value)


class TestBootstrapFastPath(_EmitBase):
    """bootstrap_identity's steady-state fast path reads ids with the stdlib
    only — no contracts/pydantic import — and seeds the emit context cache
    (card ce79a987 finding 2)."""

    def test_fast_path_reads_ids_without_importing_contracts(self):
        # First run creates the identity files (full contracts path).
        identity.bootstrap_identity(self.repo)
        emit._reset_ids_cache_for_tests()

        # Poison contracts so ANY contracts import raises; the fast path must not
        # touch it (both files already exist -> stdlib json read only).
        saved = {name: sys.modules.get(name) for name in ("contracts", "contracts.identity")}
        sys.modules["contracts"] = None  # type: ignore[assignment]
        sys.modules["contracts.identity"] = None  # type: ignore[assignment]
        try:
            result = identity.bootstrap_identity(self.repo)
            # Fast path returns None (models intentionally not constructed) and
            # did not raise despite contracts being unimportable.
            self.assertIsNone(result)
            # It seeded the per-process context cache from the stdlib read.
            self.assertIn(str(self.repo), emit._ids_cache)
            machine_id, repository_id, _project = emit._ids_cache[str(self.repo)]
            self.assertTrue(machine_id.startswith("mach_"))
            self.assertTrue(repository_id.startswith("repo_"))
        finally:
            for name, mod in saved.items():
                if mod is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = mod


if __name__ == "__main__":
    unittest.main()
