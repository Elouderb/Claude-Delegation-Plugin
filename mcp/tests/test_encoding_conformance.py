"""Repo-wide UTF-8 encoding conformance test (WINDOWS.md §3, enforced).

WINDOWS.md §3 established the rule: *every* text read/write in this codebase
must pass ``encoding="utf-8"`` explicitly, because the platform default is
cp1252 on Windows and an unencoded write silently truncates a file to 0 bytes on
the first non-Latin-1 character. This test converts that rule into an enforced
invariant so a future edit cannot quietly regress it.

What it flags (AST-based, so it correctly handles multi-line calls that a
line-oriented grep gets wrong):

  * builtin ``open(...)`` in a TEXT mode (no ``"b"`` in the mode) lacking
    ``encoding=``;
  * ``Path.open(...)`` in a text mode lacking ``encoding=`` (``os.open`` — which
    returns a raw fd and takes no encoding — is excluded);
  * ``.read_text(...)`` / ``.write_text(...)`` lacking ``encoding=`` (these are
    always text — there is no binary variant);
  * any call passing ``text=True`` (subprocess) without ``encoding=``.

Binary opens (``"rb"``/``"wb"``/…), ``os.open``, ``.read_bytes()`` /
``.write_bytes()``, and Flask's ``get_data(as_text=True)`` are correctly NOT
flagged.

Scope: the whole repo *.py, minus generated / vendored / non-module trees
(see ``EXCLUDED_DIRS``). ``templates/`` is excluded deliberately — it holds the
sibling config card's launcher/config templates, which may contain
placeholder-substituted, non-importable Python and whose encoding discipline is
that card's responsibility (it is also actively rewritten concurrently, so
scanning it would make this test flaky). ``ALLOWLIST`` is the explicit,
documented escape hatch for any deliberate binary/platform case; it is currently
empty because the sweep left no legitimate exceptions.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from typing import List, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories never scanned: virtualenvs, vendored deps, generated graph output,
# VCS/pycache/build artifacts, and the sibling-owned config templates tree.
EXCLUDED_DIRS = {
    ".venv", "venv", "node_modules", "graphify-out", ".git",
    "__pycache__", "build", "dist", "templates",
}

# Explicit, documented allow-list of deliberate exceptions, as
# (repo-relative POSIX path, lineno) tuples. Empty by design: the UTF-8 sweep
# left no text-mode I/O that legitimately omits encoding=. Add an entry ONLY for
# a genuine binary/platform case that the AST heuristics cannot classify, with a
# comment explaining why.
ALLOWLIST: set[Tuple[str, int]] = set()

# A string constant that is a real file mode (so an attribute ``.open("http://")``
# — e.g. a hypothetical webbrowser.open — is not mistaken for a file open).
_FILE_MODE_RE = re.compile(r"^[rwxa][btU+]*$")


def _iter_py_files() -> List[Path]:
    files = []
    for path in _REPO_ROOT.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in path.relative_to(_REPO_ROOT).parts):
            continue
        files.append(path)
    return files


def _has_encoding(call: ast.Call) -> bool:
    """True if the call passes encoding=, or **unpacks kwargs (undecidable →
    treated as present so we never emit a false positive)."""
    for kw in call.keywords:
        if kw.arg == "encoding":
            return True
        if kw.arg is None:  # **kwargs — cannot statically prove absence
            return True
    return False


def _mode_string(call: ast.Call, builtin_open: bool):
    """Return the file mode as a str if given as a literal, else None.

    builtin ``open(file, mode, ...)`` carries mode at positional index 1;
    ``Path.open(mode, ...)`` carries it at index 0. A ``mode=`` keyword overrides.
    """
    mode_node = None
    if builtin_open:
        if len(call.args) >= 2:
            mode_node = call.args[1]
    else:
        if len(call.args) >= 1:
            mode_node = call.args[0]
    for kw in call.keywords:
        if kw.arg == "mode":
            mode_node = kw.value
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        return mode_node.value
    return None


def _text_true(call: ast.Call) -> bool:
    """True if the call passes text=True (note: as_text=True has arg 'as_text')."""
    for kw in call.keywords:
        if kw.arg != "text":
            continue
        val = kw.value
        # getattr sidesteps a pyright ast-stub narrowing quirk on Constant.value.
        if isinstance(val, ast.Constant) and getattr(val, "value", None) is True:
            return True
    return False


def _violations_in(path: Path):
    """Yield (lineno, kind) violations for one file. SyntaxError → no violations
    (unparseable files — e.g. a concurrently-written script — are skipped)."""
    try:
        tree = ast.parse(path.read_bytes())
    except SyntaxError:
        return

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call = node
        func = call.func
        encoding_present = _has_encoding(call)
        lineno = getattr(call, "lineno", 0)

        # subprocess text=True (mutually exclusive with the func-based checks
        # below: its func is .run/.Popen, never open/read_text/write_text).
        if _text_true(call) and not encoding_present:
            yield (lineno, "text=True subprocess without encoding=")
            continue

        is_builtin_open = isinstance(func, ast.Name) and func.id == "open"
        is_attr_open = isinstance(func, ast.Attribute) and func.attr == "open"
        # os.open returns a raw file descriptor and takes no encoding — exclude.
        # (The redundant isinstance re-narrows func for the type checker.)
        if (
            is_attr_open
            and isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "os"
        ):
            is_attr_open = False

        if is_builtin_open or is_attr_open:
            mode = _mode_string(call, builtin_open=is_builtin_open)
            if mode is not None and "b" in mode.lower():
                continue  # binary open — no encoding applies
            # A non-file-mode string on an attribute .open is not a file open.
            if is_attr_open and mode is not None and not _FILE_MODE_RE.match(mode):
                continue
            if not encoding_present:
                yield (lineno, "text-mode open() without encoding=")
            continue

        if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
            if not encoding_present:
                yield (lineno, f"{func.attr}() without encoding=")


class TestEncodingConformance(unittest.TestCase):
    def test_no_unencoded_text_io(self):
        offences = []
        for path in _iter_py_files():
            rel = path.relative_to(_REPO_ROOT).as_posix()
            for lineno, kind in _violations_in(path):
                if (rel, lineno) in ALLOWLIST:
                    continue
                offences.append(f"{rel}:{lineno}: {kind}")

        self.assertEqual(
            offences,
            [],
            "Text I/O without encoding=\"utf-8\" found (WINDOWS.md §3). Add "
            "encoding=\"utf-8\", switch to a binary read/write, or (only for a "
            "genuine binary/platform case) add the site to ALLOWLIST:\n  "
            + "\n  ".join(offences),
        )

    def test_scanner_catches_a_known_violation(self):
        # Guard the guard: run the REAL scanner (_violations_in) over a tmp sample
        # so a regression in the actual predicates can't slip past a
        # re-implemented copy. The sample also includes a binary open and an
        # already-encoded open, which must NOT be flagged.
        import tempfile

        sample = (
            "open('f', 'w')\n"                    # text-mode open(), no encoding
            "p.read_text()\n"                     # read_text(), no encoding
            "subprocess.run(x, text=True)\n"      # text=True subprocess, no encoding
            "open('f', 'rb')\n"                   # binary open — must NOT flag
            "open('f', 'w', encoding='utf-8')\n"  # encoded — must NOT flag
        )
        with tempfile.TemporaryDirectory() as tmp:
            sample_path = Path(tmp) / "sample.py"
            sample_path.write_text(sample, encoding="utf-8")
            kinds = sorted(kind for _lineno, kind in _violations_in(sample_path))

        self.assertEqual(
            kinds,
            sorted(
                [
                    "read_text() without encoding=",
                    "text-mode open() without encoding=",
                    "text=True subprocess without encoding=",
                ]
            ),
        )


if __name__ == "__main__":
    unittest.main()
