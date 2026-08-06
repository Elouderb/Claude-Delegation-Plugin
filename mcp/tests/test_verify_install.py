"""
Tests for installer/verify_install.py.

Includes a POSIX end-to-end handshake against the current interpreter's task-cards
server (initialize + tools/list must report 27 tools), plus fast unit coverage of
${CLAUDE_PLUGIN_ROOT} expansion and the no-hang failure path. The e2e test spawns
the real mcp/server.py, so it is skipped when the mcp package is not importable
(mirroring how the db_* tests skip without a SQL Server) and on Windows (the card
scopes the e2e test to POSIX; the verifier code itself is cross-platform).
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_INSTALLER_DIR = _REPO_ROOT / "installer"
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_MCP_DIR = _REPO_ROOT / "mcp"
sys.path.insert(0, str(_INSTALLER_DIR))
sys.path.insert(0, str(_SCRIPTS_DIR))
sys.path.insert(0, str(_MCP_DIR))

import generate_config as gc  # noqa: E402
import smoke_test  # noqa: E402  (reuse its _free_port helper — one copy, not three)
import verify_install as vi  # noqa: E402

_MCP_AVAILABLE = importlib.util.find_spec("mcp") is not None
# Single source of truth for the tool count: the verifier OWNS the constant; the
# test reads it rather than redefining a number that could silently drift.
EXPECTED_TOOLS = vi.DEFAULT_EXPECTED_TOOLS

_free_port = smoke_test._free_port


class TestLoadCommand(unittest.TestCase):
    def test_plugin_root_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            mcp_json = Path(tmp) / ".mcp.json"
            mcp_json.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "task-cards": {
                                "command": "/venv/bin/python",
                                "args": ["${CLAUDE_PLUGIN_ROOT}/mcp/server.py"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            command, args, env = vi.load_command(
                mcp_json, "task-cards", Path("/plugin/here")
            )
            # str(Path(...)) renders platform separators (backslashes on
            # Windows) — production-correct, so the expectation must match.
            root = str(Path("/plugin/here"))
            self.assertEqual(command, "/venv/bin/python")
            self.assertEqual(args, [f"{root}/mcp/server.py"])
            self.assertEqual(env["CLAUDE_PLUGIN_ROOT"], root)

    def test_missing_server_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            mcp_json = Path(tmp) / ".mcp.json"
            mcp_json.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
            with self.assertRaises(vi.HandshakeError):
                vi.load_command(mcp_json, "task-cards", Path(tmp))

    def test_missing_file_raises(self):
        with self.assertRaises(vi.HandshakeError):
            vi.load_command(Path("/no/such/.mcp.json"), "task-cards", Path("/"))


class TestHandshakeFailurePath(unittest.TestCase):
    def test_process_that_never_speaks_mcp_fails_cleanly(self):
        # A child that exits immediately must yield ok=False quickly (no hang),
        # exercising the "server closed stdout" path.
        result = vi.handshake(
            sys.executable,
            ["-c", "import sys; sys.exit(0)"],
            expected_tools=EXPECTED_TOOLS,
            timeout=10.0,
        )
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)

    def test_missing_interpreter_fails_cleanly(self):
        # A stale/removed interpreter path (Popen FileNotFoundError) must return a
        # clean ok=False, never a raw traceback.
        result = vi.handshake(
            str(_REPO_ROOT / "no" / "such" / "python-xyz"),
            [],
            expected_tools=EXPECTED_TOOLS,
            timeout=10.0,
        )
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.error)
        assert result.error is not None
        self.assertIn("could not spawn", result.error)

    def test_child_that_dies_at_import_reports_stderr_tail(self):
        # A server that crashes on startup: the failure must be clean AND carry
        # the captured server-stderr tail so the operator sees the real cause,
        # whether the break surfaces as a broken stdin pipe or a closed stdout.
        result = vi.handshake(
            sys.executable,
            ["-c", "import sys; sys.stderr.write('IMPORT BOOM\\n'); sys.exit(1)"],
            expected_tools=EXPECTED_TOOLS,
            timeout=10.0,
        )
        self.assertFalse(result.ok)
        self.assertIn("IMPORT BOOM", result.stderr)


class TestCheckGraphify(unittest.TestCase):
    """Unit coverage for the soft post-install ``graphify``-resolution check
    (card 43ba7d28). Uses the same os.name-conditional path shape as the
    function under test, so a single test body is correct on POSIX and
    Windows without patching ``os.name`` itself (unlike the platform-specific
    ``sys.executable`` fixtures in test_resolve_graphify.py's battery)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="agent_os_checkgraphify_")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.tmp = Path(self._tmp)
        self._bin_name = "Scripts" if os.name == "nt" else "bin"
        self._graphify_name = "graphify.exe" if os.name == "nt" else "graphify"
        # Never let a stray AGENT_OS_GRAPHIFY_EXECUTABLE from the outer
        # environment leak into a test that doesn't set it explicitly.
        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("AGENT_OS_GRAPHIFY_EXECUTABLE", None)
        self.addCleanup(self._env_patch.stop)

    def test_env_override_existing_file_resolves(self):
        override = self.tmp / "custom-graphify"
        override.write_text("", encoding="utf-8")
        os.environ["AGENT_OS_GRAPHIFY_EXECUTABLE"] = str(override)
        self.assertIsNone(vi.check_graphify(self.tmp))

    def test_env_override_missing_reports_diagnostic(self):
        os.environ["AGENT_OS_GRAPHIFY_EXECUTABLE"] = str(self.tmp / "nope")
        with patch.object(shutil, "which", return_value=None):
            issue = vi.check_graphify(self.tmp)
        self.assertIsNotNone(issue)
        self.assertIn("AGENT_OS_GRAPHIFY_EXECUTABLE", issue)

    def test_found_beside_venv_interpreter(self):
        exe_dir = self.tmp / ".venv" / self._bin_name
        exe_dir.mkdir(parents=True)
        (exe_dir / self._graphify_name).write_text("", encoding="utf-8")
        with patch.object(shutil, "which", return_value=None):
            self.assertIsNone(vi.check_graphify(self.tmp))

    def test_path_fallback_resolves(self):
        with patch.object(shutil, "which", return_value="/usr/local/bin/graphify"):
            self.assertIsNone(vi.check_graphify(self.tmp))

    def test_not_found_anywhere_reports_diagnostic(self):
        with patch.object(shutil, "which", return_value=None):
            issue = vi.check_graphify(self.tmp)
        self.assertIsNotNone(issue)
        self.assertIn("graphify", issue)
        self.assertIn("mcp/graph_requirements.txt", issue)


@unittest.skipIf(os.name == "nt", "POSIX end-to-end handshake (card-scoped)")
@unittest.skipUnless(_MCP_AVAILABLE, "mcp package not installed")
class TestHandshakeEndToEnd(unittest.TestCase):
    def test_generated_config_reports_27_tools(self):
        interpreter = sys.executable
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mcp_json = tmp_path / ".mcp.json"
            # Render the real committed template for the current interpreter.
            data = gc.render_mcp(gc.MCP_TEMPLATE, interpreter)
            mcp_json.write_text(json.dumps(data, indent=2), encoding="utf-8")

            child_cwd = tmp_path / "cwd"
            child_cwd.mkdir()
            # The spawned server runs ensure_agent_os(), whose Phase 1b identity
            # bootstrap would otherwise mint/pollute the REAL ~/.agent-os. Point
            # the child (which inherits os.environ) at a temp home.
            child_home = tmp_path / "home"
            child_home.mkdir()

            # Isolate the child's Flask graph server on a free port so it never
            # collides with a graph server already running on the default port.
            prev_port = os.environ.get("AGENT_OS_GRAPH_PORT")
            prev_home = os.environ.get("AGENT_OS_HOME")
            os.environ["AGENT_OS_GRAPH_PORT"] = str(_free_port())
            os.environ["AGENT_OS_HOME"] = str(child_home)
            try:
                result = vi.verify(
                    mcp_json,
                    plugin_root=_REPO_ROOT,
                    expected_tools=EXPECTED_TOOLS,
                    cwd=child_cwd,
                    timeout=90.0,
                )
            finally:
                if prev_port is None:
                    os.environ.pop("AGENT_OS_GRAPH_PORT", None)
                else:
                    os.environ["AGENT_OS_GRAPH_PORT"] = prev_port
                if prev_home is None:
                    os.environ.pop("AGENT_OS_HOME", None)
                else:
                    os.environ["AGENT_OS_HOME"] = prev_home

            detail = result.error or ""
            if not result.ok and result.stderr:
                detail += "\nserver stderr tail:\n" + "\n".join(
                    result.stderr.strip().splitlines()[-15:]
                )
            self.assertTrue(result.ok, detail)
            self.assertEqual(result.tool_count, EXPECTED_TOOLS, detail)


if __name__ == "__main__":
    unittest.main()
