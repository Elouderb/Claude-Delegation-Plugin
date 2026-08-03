"""
Tests for scripts/generate_config.py.

Covers substitution correctness (the interpreter, and nothing else, changes),
POSIX vs. Windows shell-quoting of the hook interpreter, valid-JSON output, a
strict round-trip against BOTH committed templates (generated == template with
the interpreter swapped back to the placeholder), idempotency, and the
missing-template error.

All tests write to temporary directories; none touch the real repo's .mcp.json
or hooks/hooks.json.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# generate_config.py lives at scripts/ (two levels up from this tests/ package).
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import generate_config as gc  # noqa: E402

POSIX_INTERP = "/opt/agent-os/.venv/bin/python"
WIN_INTERP = r"C:\Users\Test User\proj\.venv\Scripts\python.exe"


def _hook_commands(hooks: dict) -> list[str]:
    commands: list[str] = []
    for blocks in hooks["hooks"].values():
        for block in blocks:
            for hook in block["hooks"]:
                commands.append(hook["command"])
    return commands


class TestMcpRender(unittest.TestCase):
    def test_command_substituted_raw(self):
        data = gc.render_mcp(gc.MCP_TEMPLATE, POSIX_INTERP)
        entry = data["mcpServers"]["task-cards"]
        # command is argv[0] (not a shell string) -> raw path, no quoting.
        self.assertEqual(entry["command"], POSIX_INTERP)
        self.assertEqual(entry["args"], ["${CLAUDE_PLUGIN_ROOT}/mcp/server.py"])
        self.assertNotIn(gc.PLACEHOLDER, json.dumps(data))

    def test_windows_backslashes_survive_json(self):
        data = gc.render_mcp(gc.MCP_TEMPLATE, WIN_INTERP)
        # Round-tripped through JSON the decoded value must equal the raw path
        # (backslashes correctly escaped on write, unescaped on read).
        text = json.dumps(data)
        self.assertEqual(json.loads(text)["mcpServers"]["task-cards"]["command"], WIN_INTERP)


class TestHooksRender(unittest.TestCase):
    def test_posix_quoting_and_substitution(self):
        data = gc.render_hooks(gc.HOOKS_TEMPLATE, POSIX_INTERP, windows=False)
        commands = _hook_commands(data)
        # The current file carries 9 command strings across 8 event types.
        self.assertEqual(len(commands), 9)
        for cmd in commands:
            self.assertNotIn(gc.PLACEHOLDER, cmd)
            self.assertIn(POSIX_INTERP, cmd)
            # ${CLAUDE_PLUGIN_ROOT} is a Claude Code expansion -> must be preserved.
            self.assertIn("${CLAUDE_PLUGIN_ROOT}", cmd)
            # A plain POSIX path has no special chars -> left unquoted, and the
            # command begins with the interpreter.
            self.assertTrue(cmd.startswith(POSIX_INTERP + " "))

    def test_windows_interpreter_is_double_quoted(self):
        data = gc.render_hooks(gc.HOOKS_TEMPLATE, WIN_INTERP, windows=True)
        for cmd in _hook_commands(data):
            self.assertNotIn(gc.PLACEHOLDER, cmd)
            # The spaced Windows path must be double-quoted so cmd.exe keeps it
            # as a single token.
            self.assertTrue(cmd.startswith('"' + WIN_INTERP + '"'))

    def test_shell_quote_helper(self):
        self.assertEqual(gc.shell_quote(WIN_INTERP, windows=True), '"' + WIN_INTERP + '"')
        # POSIX: a path with a space gets shell-quoted; a plain path does not.
        self.assertEqual(gc.shell_quote(POSIX_INTERP, windows=False), POSIX_INTERP)
        self.assertNotEqual(
            gc.shell_quote("/a b/python", windows=False), "/a b/python"
        )


class TestRoundTripBothTemplates(unittest.TestCase):
    """Generated file == template with only the interpreter substituted."""

    def test_mcp_round_trip(self):
        template = json.loads(gc.MCP_TEMPLATE.read_text(encoding="utf-8"))
        rendered = gc.render_mcp(gc.MCP_TEMPLATE, POSIX_INTERP)
        # Swap the interpreter back to the placeholder -> must equal the template.
        restored = json.loads(
            json.dumps(rendered).replace(json.dumps(POSIX_INTERP)[1:-1], gc.PLACEHOLDER)
        )
        self.assertEqual(restored, template)

    def test_hooks_round_trip(self):
        template = json.loads(gc.HOOKS_TEMPLATE.read_text(encoding="utf-8"))
        rendered = gc.render_hooks(gc.HOOKS_TEMPLATE, POSIX_INTERP, windows=False)
        # POSIX unquoted -> swapping the raw path back to the placeholder restores
        # the template exactly (proves nothing else changed).
        restored = json.loads(json.dumps(rendered).replace(POSIX_INTERP, gc.PLACEHOLDER))
        self.assertEqual(restored, template)


class TestGenerateFiles(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.mcp_out = self.tmp / ".mcp.json"
        self.hooks_out = self.tmp / "hooks" / "hooks.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_writes_valid_json_and_creates_dirs(self):
        gc.generate(
            POSIX_INTERP,
            windows=False,
            mcp_output=self.mcp_out,
            hooks_output=self.hooks_out,
        )
        self.assertTrue(self.mcp_out.exists())
        self.assertTrue(self.hooks_out.exists())
        # Both parse as valid JSON.
        json.loads(self.mcp_out.read_text(encoding="utf-8"))
        json.loads(self.hooks_out.read_text(encoding="utf-8"))

    def test_idempotent(self):
        gc.generate(
            POSIX_INTERP, windows=False, mcp_output=self.mcp_out, hooks_output=self.hooks_out
        )
        first_mcp = self.mcp_out.read_bytes()
        first_hooks = self.hooks_out.read_bytes()
        gc.generate(
            POSIX_INTERP, windows=False, mcp_output=self.mcp_out, hooks_output=self.hooks_out
        )
        self.assertEqual(self.mcp_out.read_bytes(), first_mcp)
        self.assertEqual(self.hooks_out.read_bytes(), first_hooks)


class TestErrors(unittest.TestCase):
    def test_missing_template_raises(self):
        with self.assertRaises(FileNotFoundError):
            gc.render_mcp(Path("/nonexistent/does-not-exist.tmpl"), POSIX_INTERP)

    def test_generate_missing_template_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with self.assertRaises(FileNotFoundError):
                gc.generate(
                    POSIX_INTERP,
                    windows=False,
                    mcp_template=tmp_path / "missing.tmpl",
                    mcp_output=tmp_path / ".mcp.json",
                    hooks_output=tmp_path / "hooks.json",
                )


if __name__ == "__main__":
    unittest.main()
