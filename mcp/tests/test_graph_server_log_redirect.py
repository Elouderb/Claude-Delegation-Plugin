"""
Regression test for the stdout/stderr log-file redirect in
graph_server._spawn_graph_server (card 0637f4bf).

Background
---------
graph_server.py used to spawn the Flask graph server with
``stdout=subprocess.PIPE, stderr=subprocess.PIPE``. The parent (MCP server) only
drains those pipes via ``communicate()`` AFTER the child has exited; while the
child is ALIVE nobody reads them. The Werkzeug dev server logs every HTTP request
to stderr, so after enough request logging the child's write blocks once the
~64KB kernel pipe buffer fills, hanging the graph UI mid-session.

The fix redirects the child's stdout+stderr to a log file
(``~/.agent-os/graph_server-<port>.log``); file writes never block. Crash
diagnostics are preserved by reading a bounded tail of that file post-exit
(``_read_log_tail``) instead of ``communicate()``.

What this test does
-------------------
It calls the REAL ``graph_server._spawn_graph_server`` with a throwaway child
script and ``port=0`` — NOT app.py, NOT Flask, NOT port 5000 — so the live :5000
graph server is never touched and the log file is ``graph_server-0.log``, which
cannot clobber a real instance's log. The test removes that log file in teardown.

  * Test A (no-block): the child writes well over 64KB to BOTH stdout and stderr,
    then writes a sentinel line and exits 0. We assert the sentinel reaches the
    log file within a short timeout. Under the OLD PIPE-without-drain code this
    child would deadlock at the ~64KB boundary (its write blocks because the
    parent never reads the pipe), and the sentinel would never appear. With the
    file redirect the writes never block, so the child completes.

  * Test B (diagnostics preserved): the child writes a known marker to stderr,
    then exits non-zero. We assert ``_read_log_tail(0)`` contains the marker —
    proving crash diagnostics still work after the switch away from communicate().

Portable: a file redirect works on every platform, so this test is NOT
Linux-gated (unlike the pdeathsig test).
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

_MCP_DIR = Path(__file__).resolve().parent.parent  # mcp/
sys.path.insert(0, str(_MCP_DIR))

import graph_server  # noqa: E402  (path set above so the real module imports)

# Port used for every spawn here. 0 keeps the log file at graph_server-0.log,
# which cannot collide with the default :5000 instance's log, and the throwaway
# child never binds it anyway.
_TEST_PORT = 0

# Bytes written to EACH of stdout and stderr — comfortably past the ~64KB Linux
# pipe buffer that the old PIPE-without-drain code would deadlock on.
_BIG = 200_000
_SENTINEL = "CHILD_REACHED_END_SENTINEL"
_MARKER = "DISTINCT_CRASH_MARKER_xyz123"

# _spawn_graph_server builds its own argv ([python, app_path]) and passes no extra
# args, so each throwaway child must self-contain its parameters; the test bodies
# write fully-parameterized scripts to the temp dir rather than passing argv.


def _wait_for_proc(proc: subprocess.Popen, timeout: float) -> int | None:
    """Wait up to ``timeout`` for ``proc`` to exit; return its code or None."""
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


class TestGraphServerLogRedirect(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="agent_os_log_redirect_")
        self._tmpdir = Path(self._tmp.name)
        self._proc: subprocess.Popen | None = None
        # Path the real code will write to for this port; cleaned up in tearDown.
        self._log_path = graph_server._graph_log_path(_TEST_PORT)

    def tearDown(self):
        # Best-effort: never leak a spawned child even if an assertion failed.
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except Exception:
                pass
        # Remove the test's own log file (graph_server-0.log) so we leave no
        # residue in ~/.agent-os/.
        try:
            self._log_path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
        self._tmp.cleanup()

    def _write_child(self, name: str, src: str) -> Path:
        path = self._tmpdir / name
        path.write_text(src)
        return path

    def test_no_block_on_large_output(self):
        """>64KB on both streams must not deadlock; the sentinel must reach the log."""
        # Spawn the throwaway child via the REAL code path under test. The script
        # self-contains its parameters because _spawn_graph_server builds argv
        # itself ([python, app_path]) and passes no extra arguments.
        child = self._write_child(
            "flood.py",
            f"import sys\n"
            f"n = {_BIG}\n"
            f"chunk = 'o' * 1000\n"
            f"written = 0\n"
            f"while written < n:\n"
            f"    sys.stdout.write(chunk + '\\n')\n"
            f"    sys.stderr.write(('e' * 1000) + '\\n')\n"
            f"    written += 1000\n"
            f"sys.stdout.flush()\n"
            f"sys.stderr.flush()\n"
            f"print({_SENTINEL!r}, flush=True)\n",
        )

        self._proc = graph_server._spawn_graph_server(child, child.parent, _TEST_PORT)

        # If the child deadlocks at the pipe boundary it never exits; with the
        # file redirect it finishes quickly. Generous timeout to stay robust on
        # a loaded CI box while still failing fast on a real hang.
        code = _wait_for_proc(self._proc, timeout=20.0)
        self.assertIsNotNone(
            code,
            "child did not exit within timeout — it deadlocked writing >64KB "
            "(this is exactly the PIPE-without-drain hang the fix removes)",
        )
        self.assertEqual(code, 0, "flood child should exit cleanly")

        # The sentinel is the LAST thing written, after >64KB on both streams.
        # Its presence proves the child ran to completion rather than blocking.
        text = self._log_path.read_text(errors="replace")
        self.assertIn(
            _SENTINEL,
            text,
            "sentinel (written after the >64KB flood) missing from the log — "
            "the child did not reach the end",
        )

    def test_crash_diagnostics_preserved(self):
        """A non-zero exit's stderr marker must be readable via _read_log_tail."""
        child = self._write_child(
            "crash.py",
            f"import sys\n"
            f"sys.stderr.write({_MARKER!r} + '\\n')\n"
            f"sys.stderr.flush()\n"
            f"sys.exit(3)\n",
        )

        self._proc = graph_server._spawn_graph_server(child, child.parent, _TEST_PORT)
        code = _wait_for_proc(self._proc, timeout=10.0)
        self.assertEqual(code, 3, "crash child should exit non-zero (3)")

        tail = graph_server._read_log_tail(_TEST_PORT)
        self.assertIn(
            _MARKER,
            tail,
            "crash marker not found via _read_log_tail — post-exit diagnostics "
            "are no longer captured",
        )


if __name__ == "__main__":
    unittest.main()
