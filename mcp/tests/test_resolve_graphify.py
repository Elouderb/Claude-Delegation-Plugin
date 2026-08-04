"""Unit tests for the venv-safe ``graphify`` console-script resolver.

The resolver exists in three copies that MUST stay in lock-step because they run
in three separate processes / import contexts (WINDOWS.md §2):

  * ``scripts/hook_common.resolve_graphify`` — the lifecycle hooks,
  * ``mcp/graph_io.resolve_graphify``        — the MCP server tools,
  * ``mcp/db_tools/app._resolve_graphify``   — the standalone Flask UI process.

Every resolver here is run through the SAME battery via ``subTest`` so a drift
between the copies fails loudly. Windows behaviour (``Scripts/graphify.exe``) is
proven with tmp dirs + monkeypatched ``sys.executable`` — never a runtime probe,
since the suite runs on Linux.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Make the three resolver homes importable regardless of working directory.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
_MCP_DIR = Path(__file__).resolve().parent.parent
_DB_TOOLS_DIR = _MCP_DIR / "db_tools"
for _d in (_DB_TOOLS_DIR, _MCP_DIR, _SCRIPTS_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

import hook_common  # noqa: E402  (scripts/)
import graph_io  # noqa: E402  (mcp/ — no mcp-package import at module load)

_RESOLVERS = [
    ("hook_common", hook_common.resolve_graphify),
    ("graph_io", graph_io.resolve_graphify),
]

# app.py pulls flask / python-dotenv; import defensively so a missing optional
# dependency degrades to "skip this copy" rather than erroring the module.
try:
    import app as _db_app  # noqa: E402  (mcp/db_tools/app.py)

    _RESOLVERS.append(("app", _db_app._resolve_graphify))
except Exception:  # pragma: no cover - only when flask/dotenv absent
    pass


def _touch(path: Path) -> Path:
    path.write_text("", encoding="utf-8")
    return path


class ResolveGraphifyBattery(unittest.TestCase):
    """Run every resolver copy through an identical battery via subTest."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="agent_os_resolvetest_")
        self.addCleanup(shutil.rmtree, self._tmp, True)
        self.tmp = Path(self._tmp)

    def _run(self, env, sys_exe, which_ret):
        """Invoke each resolver under a controlled env/sys.executable/which."""
        results = {}
        for name, fn in _RESOLVERS:
            with patch.dict(os.environ, env, clear=False):
                # Ensure the override key is truly absent unless the test sets it.
                if "AGENT_OS_GRAPHIFY_EXECUTABLE" not in env:
                    os.environ.pop("AGENT_OS_GRAPHIFY_EXECUTABLE", None)
                with patch.object(sys, "executable", str(sys_exe)), \
                        patch.object(shutil, "which", return_value=which_ret):
                    results[name] = fn()
        return results

    def test_env_override_wins(self):
        # The override is used verbatim and short-circuits filesystem/PATH probing
        # — even when a graphify sits beside sys.executable AND which finds one.
        _touch(self.tmp / "graphify")
        results = self._run(
            env={"AGENT_OS_GRAPHIFY_EXECUTABLE": "/opt/custom/graphify"},
            sys_exe=self.tmp / "python",
            which_ret="/usr/bin/graphify",
        )
        for name, got in results.items():
            with self.subTest(resolver=name):
                self.assertEqual(got, "/opt/custom/graphify")

    def test_adjacent_posix_no_exe(self):
        # graphify beside sys.executable is returned via the RAW (un-resolved)
        # interpreter dir — the resolver must not .resolve() the found path away.
        _touch(self.tmp / "graphify")
        expected = str(self.tmp / "graphify")
        results = self._run(env={}, sys_exe=self.tmp / "python", which_ret=None)
        for name, got in results.items():
            with self.subTest(resolver=name):
                self.assertEqual(got, expected)

    def test_adjacent_windows_exe(self):
        _touch(self.tmp / "graphify.exe")
        expected = str(self.tmp / "graphify.exe")
        results = self._run(env={}, sys_exe=self.tmp / "python.exe", which_ret=None)
        for name, got in results.items():
            with self.subTest(resolver=name):
                self.assertEqual(got, expected)

    def test_adjacent_exe_preferred_when_both_present(self):
        # Scripts/ can hold both graphify.exe and a graphify shim; the .exe must
        # win so Windows resolves an actually-executable target.
        _touch(self.tmp / "graphify.exe")
        _touch(self.tmp / "graphify")
        expected = str(self.tmp / "graphify.exe")
        results = self._run(env={}, sys_exe=self.tmp / "python", which_ret=None)
        for name, got in results.items():
            with self.subTest(resolver=name):
                self.assertEqual(got, expected)

    @unittest.skipIf(os.name == "nt", "symlink-based venv-escape regression is POSIX")
    def test_symlinked_interpreter_probes_raw_parent_first(self):
        # THE CRITICAL regression (WINDOWS.md §2 / this fix pass). A venv's
        # .venv/bin/python is a symlink to the base interpreter; pip installs the
        # ``graphify`` console script beside the SYMLINK (raw parent), NOT beside
        # the resolved target. The old code did Path(sys.executable).resolve()
        # .parent FIRST, which followed the symlink OUT of the venv and missed the
        # console script, then fell back to a failing PATH lookup. The raw parent
        # must be probed first and win.
        venv_bin = self.tmp / "venv" / "bin"
        real_bin = self.tmp / "real" / "bin"
        venv_bin.mkdir(parents=True)
        real_bin.mkdir(parents=True)
        real_python = _touch(real_bin / "python")
        venv_python = venv_bin / "python"
        venv_python.symlink_to(real_python)
        # graphify installed ONLY beside the symlink (the venv), as pip does.
        _touch(venv_bin / "graphify")
        expected = str(venv_bin / "graphify")
        results = self._run(env={}, sys_exe=venv_python, which_ret=None)
        for name, got in results.items():
            with self.subTest(resolver=name):
                self.assertEqual(got, expected)

    @unittest.skipIf(os.name == "nt", "symlink-based venv-escape regression is POSIX")
    def test_symlinked_interpreter_falls_back_to_resolved_parent(self):
        # The complementary pyenv case: nothing beside the symlink, but a graphify
        # sits beside the RESOLVED base interpreter. The second (resolved-parent)
        # probe must find it — proving we kept coverage of the non-venv install.
        venv_bin = self.tmp / "venv" / "bin"
        real_bin = self.tmp / "real" / "bin"
        venv_bin.mkdir(parents=True)
        real_bin.mkdir(parents=True)
        real_python = _touch(real_bin / "python")
        venv_python = venv_bin / "python"
        venv_python.symlink_to(real_python)
        _touch(real_bin / "graphify")  # only beside the resolved target
        expected = str(real_bin / "graphify")
        results = self._run(env={}, sys_exe=venv_python, which_ret=None)
        for name, got in results.items():
            with self.subTest(resolver=name):
                self.assertEqual(got, expected)

    def test_path_fallback(self):
        # Nothing beside sys.executable -> fall back to PATH via shutil.which.
        results = self._run(
            env={}, sys_exe=self.tmp / "python", which_ret="/usr/local/bin/graphify"
        )
        for name, got in results.items():
            with self.subTest(resolver=name):
                self.assertEqual(got, "/usr/local/bin/graphify")

    def test_not_found_falls_back_to_bare_name(self):
        # No override, none beside sys.executable, not on PATH -> bare "graphify"
        # so the caller's FileNotFoundError handling surfaces a clear message.
        results = self._run(env={}, sys_exe=self.tmp / "python", which_ret=None)
        for name, got in results.items():
            with self.subTest(resolver=name):
                self.assertEqual(got, "graphify")


if __name__ == "__main__":
    unittest.main()
