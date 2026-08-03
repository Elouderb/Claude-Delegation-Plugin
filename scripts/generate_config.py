#!/usr/bin/env python3
"""Generate ``.mcp.json`` and ``hooks/hooks.json`` from committed templates.

The plugin's MCP server and lifecycle hooks must invoke a *known-good* Python
interpreter by absolute path.  On Windows the ambient ``python3`` resolves to a
Microsoft Store execution-alias stub rather than a real interpreter (WINDOWS.md
Section 1), so a checked-in ``python3`` command silently no-ops every hook and
never starts the server.  The permanent fix (WINDOWS.md Section 5) is to stop
checking an OS-specific interpreter path into version control at all:

* ``templates/mcp.json.tmpl`` and ``templates/hooks.json.tmpl`` are committed and
  carry an ``@@PYTHON@@`` placeholder where the interpreter belongs.
* The per-platform installers run THIS script with the plugin venv's Python, and
  it writes ``.mcp.json`` + ``hooks/hooks.json`` with that interpreter's absolute
  path substituted in.  The generated files are git-ignored.

Design notes:

* Stdlib only.  Never imports the ``mcp`` package or any third-party module, so
  it runs before dependencies are installed and stays cheap in the installer.
* All file I/O passes ``encoding="utf-8"`` explicitly (WINDOWS.md Section 3: the
  platform default on Windows is cp1252, which silently corrupts non-Latin-1
  output).
* Templating is JSON-structured (``json.load`` -> substitute -> ``json.dump``),
  not raw text replacement, so a Windows interpreter path's backslashes are
  JSON-escaped correctly and the output is always valid JSON.
* ``.mcp.json``'s ``command`` is argv[0] (executed directly, not via a shell), so
  it receives the RAW interpreter path.  Each ``hooks.json`` command is a shell
  string, so the interpreter is SHELL-QUOTED there to survive spaces in the path.
* The default interpreter is ``sys.executable``: the installer invokes this
  script *with* the venv Python, making that the single source of truth for the
  interpreter path (WINDOWS.md Section 5.3).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

PLACEHOLDER = "@@PYTHON@@"

# scripts/generate_config.py -> the plugin root is this file's parent's parent.
SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
TEMPLATES_DIR = PLUGIN_ROOT / "templates"

MCP_TEMPLATE = TEMPLATES_DIR / "mcp.json.tmpl"
HOOKS_TEMPLATE = TEMPLATES_DIR / "hooks.json.tmpl"
MCP_OUTPUT = PLUGIN_ROOT / ".mcp.json"
HOOKS_OUTPUT = PLUGIN_ROOT / "hooks" / "hooks.json"


def load_template(path: Path) -> Any:
    """Parse a committed template as JSON.

    Raises ``FileNotFoundError`` (not a bare crash later) when the template is
    missing, so callers/installers can surface a clear error.
    """
    if not path.exists():
        raise FileNotFoundError(f"Template not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _substitute(obj: Any, replacement: str) -> Any:
    """Recursively replace ``PLACEHOLDER`` in every string within ``obj``."""
    if isinstance(obj, str):
        return obj.replace(PLACEHOLDER, replacement)
    if isinstance(obj, list):
        return [_substitute(item, replacement) for item in obj]
    if isinstance(obj, dict):
        return {key: _substitute(value, replacement) for key, value in obj.items()}
    return obj


def shell_quote(interpreter: str, windows: bool) -> str:
    """Quote an interpreter path for use inside a shell command string.

    Windows hook commands run under ``cmd.exe``, which does not understand POSIX
    single-quoting; it honours double quotes, and a Windows filename can never
    contain a literal ``"``.  POSIX commands use ``shlex.quote`` (a plain path
    with no special characters is returned unquoted, matching prior behaviour).
    """
    if windows:
        return f'"{interpreter}"'
    return shlex.quote(interpreter)


def render_mcp(template_path: Path, interpreter: str) -> Any:
    """Return the ``.mcp.json`` structure with the interpreter substituted.

    ``command`` is argv[0] (executed directly, no shell), so the raw path is
    correct; ``json.dump`` handles any required escaping on write.
    """
    return _substitute(load_template(template_path), interpreter)


def render_hooks(template_path: Path, interpreter: str, windows: bool) -> Any:
    """Return the ``hooks.json`` structure with a shell-quoted interpreter."""
    return _substitute(load_template(template_path), shell_quote(interpreter, windows))


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def generate(
    interpreter: str | None = None,
    *,
    windows: bool | None = None,
    mcp_template: Path = MCP_TEMPLATE,
    hooks_template: Path = HOOKS_TEMPLATE,
    mcp_output: Path = MCP_OUTPUT,
    hooks_output: Path = HOOKS_OUTPUT,
) -> tuple[Path, Path]:
    """Generate both config files.  Returns ``(mcp_output, hooks_output)``.

    Idempotent: running it again with the same interpreter yields byte-identical
    files.  ``interpreter`` defaults to ``sys.executable``; ``windows`` defaults
    to ``os.name == "nt"`` (the platform the generated hooks will run on, which
    is the platform the installer runs on).
    """
    if interpreter is None:
        interpreter = sys.executable
    if windows is None:
        windows = os.name == "nt"

    _write_json(mcp_output, render_mcp(mcp_template, interpreter))
    _write_json(hooks_output, render_hooks(hooks_template, interpreter, windows))
    return mcp_output, hooks_output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Generate .mcp.json and hooks/hooks.json from committed templates, "
            "substituting the interpreter path for @@PYTHON@@."
        )
    )
    parser.add_argument(
        "--interpreter",
        default=sys.executable,
        help="Absolute interpreter path to embed (default: this Python, sys.executable).",
    )
    platform = parser.add_mutually_exclusive_group()
    platform.add_argument(
        "--windows",
        dest="windows",
        action="store_true",
        default=None,
        help="Force Windows-style shell quoting for hook commands.",
    )
    platform.add_argument(
        "--posix",
        dest="windows",
        action="store_false",
        help="Force POSIX-style shell quoting for hook commands.",
    )
    args = parser.parse_args(argv)

    try:
        mcp_out, hooks_out = generate(args.interpreter, windows=args.windows)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Generated {mcp_out}")
    print(f"Generated {hooks_out}")
    print(f"Interpreter: {args.interpreter}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
