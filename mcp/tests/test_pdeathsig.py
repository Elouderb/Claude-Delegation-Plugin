"""
Regression test for PR_SET_PDEATHSIG orphan-reaping in graph_server._spawn_graph_server.

Background
---------
graph_server.py spawns the Flask graph server (db_tools/app.py) via subprocess.
Cleanup used to rely solely on server.py's atexit handler, which does NOT run on
SIGKILL, default-disposition SIGTERM, or crash — so the child was reparented to
PID 1 and orphaned on :5000. _spawn_graph_server now attaches a Linux preexec_fn
that calls prctl(PR_SET_PDEATHSIG, SIGTERM) and re-checks getppid() to close the
fork/parent-death race, so the child dies when its owning process dies by ANY means.

What this test does
-------------------
It launches an intermediate "parent" Python process that imports the REAL
``graph_server._spawn_graph_server`` and uses it to spawn a long-lived *trivial
sleeper* child (NOT app.py, NOT Flask, NOT port 5000 — so the live :5000 graph
server is never touched). The parent prints the grandchild PID and then blocks.
The test SIGKILLs that parent and asserts the grandchild exits on its own within
a short timeout — which can only happen via the pdeathsig coupling, because
SIGKILL skips every userspace cleanup path.

Linux-gated: PR_SET_PDEATHSIG is Linux-specific; skipped elsewhere.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

_MCP_DIR = Path(__file__).resolve().parent.parent  # mcp/

# Trivial long-lived sleeper used as the "app" the real _spawn_graph_server
# launches. Keeps stdout/stderr quiet and just sleeps far longer than the test.
_SLEEPER_SRC = "import time\ntime.sleep(3600)\n"

# Intermediate-parent program. Run as `python3 -c <this>` with two argv:
#   argv[1] = path to mcp/ (so graph_server is importable)
#   argv[2] = path to the throwaway sleeper script (used as app_path)
# It imports the REAL _spawn_graph_server, launches the sleeper as its child,
# prints "CHILD_PID=<pid>" on a line, flushes, then blocks forever. We SIGKILL
# this process from the test; the pdeathsig coupling must then reap the child.
_PARENT_SRC = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import graph_server
app_path = Path(sys.argv[2])
proc = graph_server._spawn_graph_server(app_path, app_path.parent, 0)
print("CHILD_PID=%d" % proc.pid, flush=True)
time.sleep(3600)
"""


def _pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` currently exists (signal 0 probe)."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by someone else — still "alive" for our purposes.
        return True


def _wait_until_gone(pid: int, timeout: float) -> bool:
    """Poll until ``pid`` no longer exists or ``timeout`` elapses. Return True if gone."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


@unittest.skipIf(sys.platform != "linux", "PR_SET_PDEATHSIG is Linux-only")
class TestPdeathsigReaping(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="agent_os_pdeathsig_")
        self._sleeper = Path(self._tmp.name) / "sleeper.py"
        self._sleeper.write_text(_SLEEPER_SRC)
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
        if self._child_pid is not None and _pid_alive(self._child_pid):
            try:
                os.kill(self._child_pid, signal.SIGKILL)
            except Exception:
                pass
        self._tmp.cleanup()

    def test_child_reaped_when_parent_sigkilled(self):
        # Launch the intermediate parent, which spawns the real graph-server child.
        self._parent = subprocess.Popen(
            [sys.executable, "-c", _PARENT_SRC, str(_MCP_DIR), str(self._sleeper)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Read the grandchild PID line the parent prints once it has spawned it.
        child_pid: int | None = None
        deadline = time.monotonic() + 10.0
        if self._parent.stdout is None:
            self.fail("intermediate parent was created without a stdout pipe")
        while time.monotonic() < deadline:
            line = self._parent.stdout.readline()
            if not line:
                # Parent exited before announcing — surface its stderr.
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
        self.assertTrue(_pid_alive(child_pid), "graph-server child should be alive before kill")

        # Hard-kill the parent. SIGKILL skips atexit and every userspace cleanup,
        # so the child can only be reaped via the kernel-level pdeathsig coupling.
        self._parent.kill()
        self._parent.wait(timeout=5)

        # The child must exit on its own, promptly.
        self.assertTrue(
            _wait_until_gone(child_pid, timeout=5.0),
            f"graph-server child {child_pid} was NOT reaped after parent SIGKILL "
            f"(PR_SET_PDEATHSIG coupling did not fire)",
        )


if __name__ == "__main__":
    unittest.main()
