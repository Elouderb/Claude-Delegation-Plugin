"""Regression test for Windows kill-on-parent-exit via Job Object
(graph_server._couple_child_to_parent / _ensure_job_object) — card 2f94ecee.

Background
---------
Windows has no ``preexec_fn`` and no PR_SET_PDEATHSIG, so a companion spawned by
the MCP server ORPHANS when the parent dies (SIGKILL / crash / a /reload-plugins
teardown) — the old graph server then keeps :5000. The fix assigns every spawned
companion to a process-wide Job Object created with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; the parent is the sole holder of the job
handle, so when the parent dies the OS closes that handle and terminates every
process in the job.

What this test does (the Windows analogue of test_pdeathsig)
-----------------------------------------------------------
It launches an intermediate "parent" Python process that imports the REAL
``graph_server`` and uses ``_couple_child_to_parent`` to bind a long-lived trivial
sleeper child to the process-wide Job Object. The parent prints the child PID and
blocks. The test hard-kills the parent (TerminateProcess, which skips every
userspace cleanup) and asserts the child exits on its own within a short timeout —
which can only happen via the Job Object's kill-on-close coupling.

Windows-gated: Job Objects are Windows-only; skipped elsewhere. POSIX keeps using
the pdeathsig path (test_pdeathsig), which is unchanged.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

_MCP_DIR = Path(__file__).resolve().parent.parent  # mcp/
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import graph_server  # noqa: E402

# Intermediate-parent program. Run as `python -c <this>` with one argv:
#   argv[1] = path to mcp/ (so graph_server is importable)
# It imports the REAL graph_server, spawns a trivial long-lived sleeper as its
# child, assigns it to the kill-on-parent-exit Job Object, prints "CHILD_PID=<pid>",
# flushes, then blocks. The test TerminateProcess-es this parent; the Job Object's
# KILL_ON_JOB_CLOSE must then reap the child.
_PARENT_SRC = r"""
import sys, time, subprocess
sys.path.insert(0, sys.argv[1])
import graph_server
proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
graph_server._couple_child_to_parent(proc)
print("CHILD_PID=%d" % proc.pid, flush=True)
time.sleep(3600)
"""


def _wait_until_gone(pid: int, timeout: float) -> bool:
    """Poll until ``pid`` no longer exists or ``timeout`` elapses. Return True if gone."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not graph_server._pid_alive(pid):
            return True
        time.sleep(0.05)
    return not graph_server._pid_alive(pid)


@unittest.skipUnless(os.name == "nt", "Windows Job Objects are Windows-only")
class TestJobObjectReaping(unittest.TestCase):
    def setUp(self):
        self._parent: subprocess.Popen | None = None
        self._child_pid: int | None = None

    def tearDown(self):
        # Best-effort cleanup so a failed assertion never leaks processes.
        if self._parent is not None and self._parent.poll() is None:
            try:
                self._parent.kill()
                self._parent.wait(timeout=5)
            except Exception:
                pass
        if self._child_pid is not None and graph_server._pid_alive(self._child_pid):
            try:
                os.kill(self._child_pid, 9)  # TerminateProcess on Windows
            except Exception:
                pass

    def test_child_reaped_when_parent_killed(self):
        self._parent = subprocess.Popen(
            [sys.executable, "-c", _PARENT_SRC, str(_MCP_DIR)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

        # Read the child PID line the parent prints once it has spawned + coupled it.
        child_pid: int | None = None
        deadline = time.monotonic() + 15.0
        if self._parent.stdout is None:
            self.fail("intermediate parent was created without a stdout pipe")
        while time.monotonic() < deadline:
            line = self._parent.stdout.readline()
            if not line:
                if self._parent.poll() is not None:
                    err = self._parent.stderr.read() if self._parent.stderr else ""
                    self.fail(f"intermediate parent exited early; stderr:\n{err}")
                continue
            if line.startswith("CHILD_PID="):
                child_pid = int(line.strip().split("=", 1)[1])
                break

        if child_pid is None:
            self.fail("did not receive CHILD_PID from intermediate parent")
        self._child_pid = child_pid
        self.assertTrue(graph_server._pid_alive(child_pid),
                        "companion child should be alive before the parent is killed")

        # Hard-kill the parent (TerminateProcess skips every userspace cleanup), so
        # the child can only be reaped via the Job Object's kill-on-close coupling.
        self._parent.kill()
        self._parent.wait(timeout=5)

        self.assertTrue(
            _wait_until_gone(child_pid, timeout=10.0),
            f"companion child {child_pid} was NOT reaped after the parent was killed "
            f"(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE coupling did not fire)",
        )


if __name__ == "__main__":
    unittest.main()
