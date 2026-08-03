"""Unit tests for smoke_test's venv path construction.

WINDOWS.md §4: the fresh-install smoke test hardcoded POSIX ``bin/pip`` /
``bin/python`` and a ``bin/`` PATH entry, so it could not run on Windows at all.
Path building is now an ``os_name``-parameterized pure function; here we prove
both platform layouts without a real Windows box.
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_MCP_DIR = Path(__file__).resolve().parent.parent
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

import smoke_test  # noqa: E402


class TestVenvPathConstruction(unittest.TestCase):
    VENV = Path("/proj/.venv")

    def test_posix_layout(self):
        self.assertEqual(smoke_test._venv_bin_dir(self.VENV, "posix"), self.VENV / "bin")
        self.assertEqual(
            smoke_test._venv_executable(self.VENV, "python", "posix"),
            self.VENV / "bin" / "python",
        )
        self.assertEqual(
            smoke_test._venv_executable(self.VENV, "pip", "posix"),
            self.VENV / "bin" / "pip",
        )

    def test_windows_layout(self):
        # Windows: Scripts/ dir and .exe suffix on console scripts.
        self.assertEqual(smoke_test._venv_bin_dir(self.VENV, "nt"), self.VENV / "Scripts")
        self.assertEqual(
            smoke_test._venv_executable(self.VENV, "python", "nt"),
            self.VENV / "Scripts" / "python.exe",
        )
        self.assertEqual(
            smoke_test._venv_executable(self.VENV, "pip", "nt"),
            self.VENV / "Scripts" / "pip.exe",
        )

    def test_default_os_name_matches_current_platform(self):
        # With no os_name argument it must track the running platform.
        expected_dir = "Scripts" if os.name == "nt" else "bin"
        self.assertEqual(smoke_test._venv_bin_dir(self.VENV).name, expected_dir)


if __name__ == "__main__":
    unittest.main()
