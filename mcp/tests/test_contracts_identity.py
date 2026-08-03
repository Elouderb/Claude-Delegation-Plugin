"""Tests for contracts.identity — idempotent, atomic, race-safe creation (AC3).

Every test overrides ``AGENT_OS_HOME`` to a temp dir and uses a temp repo root,
so the real ``~/.agent-os`` is never touched (mirrors the AGENT_OS_MEMORY_HOME
test-override precedent).
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from contracts import identity  # noqa: E402


class _HomeOverride(unittest.TestCase):
    """Base: point AGENT_OS_HOME at a temp dir and provide a temp repo root."""

    def setUp(self):
        self._home = tempfile.TemporaryDirectory(prefix="agent_os_home_")
        self._repo = tempfile.TemporaryDirectory(prefix="agent_os_repo_")
        self._prev = os.environ.get("AGENT_OS_HOME")
        os.environ["AGENT_OS_HOME"] = self._home.name
        self.home_dir = Path(self._home.name)
        self.repo_dir = Path(self._repo.name)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("AGENT_OS_HOME", None)
        else:
            os.environ["AGENT_OS_HOME"] = self._prev
        self._home.cleanup()
        self._repo.cleanup()


class TestMachineIdentity(_HomeOverride):
    def test_home_and_path_respect_override(self):
        self.assertEqual(identity.machine_home(), self.home_dir)
        path = identity.machine_identity_path()
        self.assertEqual(path, self.home_dir / "identity.json")
        # never the real home
        self.assertFalse(str(path).startswith(str(Path.home() / ".agent-os")))

    def test_creates_valid_identity(self):
        ident = identity.ensure_machine_identity(name="m1", os_name="Linux")
        self.assertTrue(ident.machine_id.startswith("mach_"))
        self.assertEqual(ident.name, "m1")
        self.assertEqual(ident.os, "Linux")
        self.assertTrue(identity.machine_identity_path().exists())

    def test_idempotent_same_ids(self):
        a = identity.ensure_machine_identity()
        raw_after_first = identity.machine_identity_path().read_text(encoding="utf-8")
        b = identity.ensure_machine_identity()
        self.assertEqual(a.machine_id, b.machine_id)
        self.assertEqual(a, b)
        # file content unchanged by the second (no-op) call
        self.assertEqual(
            raw_after_first,
            identity.machine_identity_path().read_text(encoding="utf-8"),
        )

    def test_corrupt_fresh_identity_surfaces(self):
        # A corrupt file with a *fresh* mtime is assumed to be a live winner
        # mid-write; after the (short) claim budget it surfaces as an error rather
        # than being silently overwritten.
        path = identity.machine_identity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not valid json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            identity.ensure_machine_identity()

    def test_killed_winner_self_heals(self):
        # Simulate a winner killed between claim and completion: an empty final
        # file with an OLD mtime. Once stale it must be reclaimed and a valid
        # identity minted, so a dead winner never bricks machine identity.
        path = identity.machine_identity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")  # empty => invalid
        old = time.time() - 60
        os.utime(path, (old, old))  # make it stale
        ident = identity.ensure_machine_identity(name="m", os_name="Linux")
        self.assertTrue(ident.machine_id.startswith("mach_"))
        parsed = identity.MachineIdentity.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        self.assertEqual(parsed.machine_id, ident.machine_id)

    def test_no_temp_or_lock_leftovers(self):
        # The final file only ever appears fully-written; the claim temp/lock
        # files must be cleaned up, leaving just a valid identity.json.
        identity.ensure_machine_identity()
        leftovers = [
            p.name
            for p in self.home_dir.iterdir()
            if p.name.endswith(".tmp") or p.name.endswith(".lock")
        ]
        self.assertEqual(leftovers, [], leftovers)
        identity.MachineIdentity.model_validate_json(
            identity.machine_identity_path().read_text(encoding="utf-8")
        )

    def test_concurrent_first_run_single_winner(self):
        n = 12
        barrier = threading.Barrier(n)
        results: list = []
        lock = threading.Lock()

        def worker():
            barrier.wait()  # maximize the race on first creation
            ident = identity.ensure_machine_identity()
            with lock:
                results.append(ident.machine_id)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(results), n)
        self.assertEqual(len(set(results)), 1, f"multiple ids minted: {set(results)}")
        # exactly one identity file, containing that single id
        self.assertTrue(identity.machine_identity_path().exists())
        parsed = identity.MachineIdentity.model_validate_json(
            identity.machine_identity_path().read_text(encoding="utf-8")
        )
        self.assertEqual(parsed.machine_id, results[0])


class TestRepositoryIdentity(_HomeOverride):
    def test_creates_under_agent_os_dir(self):
        ident = identity.ensure_repository_identity(self.repo_dir)
        path = identity.repository_identity_path(self.repo_dir)
        self.assertEqual(path, self.repo_dir / ".agent-os" / "identity.json")
        self.assertTrue(path.exists())
        self.assertTrue(ident.repository_id.startswith("repo_"))
        self.assertIsNone(ident.project_id)

    def test_project_id_stamped_on_creation(self):
        pid = identity.new_project_id()
        self.assertTrue(pid.startswith("proj_"))
        ident = identity.ensure_repository_identity(self.repo_dir, project_id=pid)
        self.assertEqual(ident.project_id, pid)

    def test_idempotent_and_stable_project_id(self):
        first = identity.ensure_repository_identity(
            self.repo_dir, project_id=identity.new_project_id()
        )
        # a later call with a *different* project id does not mutate stable identity
        second = identity.ensure_repository_identity(
            self.repo_dir, project_id=identity.new_project_id()
        )
        self.assertEqual(first.repository_id, second.repository_id)
        self.assertEqual(first.project_id, second.project_id)

    def test_repo_and_machine_ids_are_distinct(self):
        m = identity.ensure_machine_identity()
        r = identity.ensure_repository_identity(self.repo_dir)
        self.assertNotEqual(m.machine_id, r.repository_id)

    def test_stamps_canonical_remote_and_machine_id(self):
        ident = identity.ensure_repository_identity(
            self.repo_dir,
            canonical_remote="git@github.com:Acme/Widgets.git",
            machine_id="mach_abc",
        )
        # canonical_remote is normalized (host/owner/repo, lowercased, no .git)
        self.assertEqual(ident.canonical_remote, "github.com/acme/widgets")
        self.assertEqual(ident.machine_id, "mach_abc")
        # defaults when not supplied
        other = tempfile.TemporaryDirectory(prefix="agent_os_repo2_")
        self.addCleanup(other.cleanup)
        plain = identity.ensure_repository_identity(Path(other.name))
        self.assertIsNone(plain.canonical_remote)
        self.assertIsNone(plain.machine_id)

    def test_writes_gitignore_sentinel(self):
        # The repo-local .agent-os/ must self-protect so identity.json is never
        # committable (the 0.2.7 git-driven-loss class).
        identity.ensure_repository_identity(self.repo_dir)
        sentinel = self.repo_dir / ".agent-os" / ".gitignore"
        self.assertTrue(sentinel.exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "*\n")


class TestCanonicalRemote(unittest.TestCase):
    def test_equivalent_remotes_normalize_equal(self):
        forms = [
            "https://github.com/Acme/Widgets.git",
            "https://github.com/acme/widgets",
            "git@github.com:Acme/Widgets.git",
            "ssh://git@github.com/acme/widgets",
            "https://user:token@github.com/acme/widgets.git",
            "  HTTPS://GitHub.com/Acme/Widgets.git/  ",
        ]
        normalized = {identity.canonical_remote_url(f) for f in forms}
        self.assertEqual(normalized, {"github.com/acme/widgets"})

    def test_empty_remote_normalizes_to_empty(self):
        self.assertEqual(identity.canonical_remote_url("   "), "")


if __name__ == "__main__":
    unittest.main()
