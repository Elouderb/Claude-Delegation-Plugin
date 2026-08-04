"""Generate and verify the committed JSON Schemas for the contract models.

The pydantic models are the source of truth (ruling 1); the JSON Schemas under
``contracts/schemas/*.json`` are *generated artifacts* committed alongside them
so non-Python consumers can validate payloads and so schema changes are visible
in review.  A drift test regenerates in-memory and asserts byte-for-byte
equality with the committed files (fails CI if someone edits a model without
regenerating).

Determinism: schemas are emitted with ``json.dumps(..., indent=2,
sort_keys=True)`` and a trailing newline, so output is stable across runs for a
given pydantic version (verified empirically before adoption).  Line endings are
forced to ``\n`` on write and normalized before the ``--check`` comparison, so a
Windows ``core.autocrlf=true`` checkout does not report every schema as drifted
(a committed ``.gitattributes`` also pins ``contracts/schemas/*.json`` to LF).

Usage::

    python -m contracts.generate_schemas          # (re)write all schemas
    python -m contracts.generate_schemas --check   # verify, exit 1 on drift
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Type

from . import ALL_MODELS
from .base import AgentOSModel

# Directory holding the committed schemas (a sibling of this module).
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


def schema_json(model: Type[AgentOSModel]) -> str:
    """Return the canonical, deterministic JSON Schema text for ``model``."""
    schema = model.model_json_schema()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def schema_filename(model: Type[AgentOSModel]) -> str:
    """The committed schema filename for ``model`` (``<ClassName>.json``)."""
    return f"{model.__name__}.json"


def _normalize_newlines(text: str) -> str:
    """Collapse CRLF / CR line endings to LF for platform-independent comparison."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (tmp + replace), UTF-8, LF newlines.

    Mirrors ``build_db_graph.atomic_write_text``: write a sibling ``.tmp`` then
    ``os.replace`` it into place so a reader never observes a half-written file.
    ``newline="\\n"`` forces LF on every platform, so regenerating on a Windows
    checkout does not rewrite the schemas with CRLF and dirty the tree.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    os.replace(tmp, path)


def write_all(target_dir: Optional[Path] = None) -> List[Path]:
    """Write every model's schema into ``target_dir`` (default ``SCHEMA_DIR``).

    Returns the list of paths written.
    """
    target = Path(target_dir) if target_dir is not None else SCHEMA_DIR
    target.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for model in ALL_MODELS:
        path = target / schema_filename(model)
        _atomic_write_text(path, schema_json(model))
        written.append(path)
    return written


def check_all(target_dir: Optional[Path] = None) -> List[str]:
    """Return a list of human-readable drift descriptions (empty == in sync).

    Detects three kinds of drift: a missing schema file, a changed schema file,
    and a stale ``.json`` in the directory with no corresponding model.
    """
    target = Path(target_dir) if target_dir is not None else SCHEMA_DIR
    problems: List[str] = []
    expected_names = set()
    for model in ALL_MODELS:
        name = schema_filename(model)
        expected_names.add(name)
        path = target / name
        want = schema_json(model)
        if not path.exists():
            problems.append(f"missing schema: {name}")
            continue
        have = path.read_text(encoding="utf-8")
        # Normalize line endings so a CRLF (autocrlf) checkout is not false-drift.
        if _normalize_newlines(have) != _normalize_newlines(want):
            problems.append(f"schema out of date: {name}")
    if target.exists():
        for path in sorted(target.glob("*.json")):
            if path.name not in expected_names:
                problems.append(f"stale schema (no model): {path.name}")
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate contract JSON Schemas.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify committed schemas match the models; exit 1 on drift.",
    )
    args = parser.parse_args(argv)

    if args.check:
        problems = check_all()
        if problems:
            print("JSON Schema drift detected:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            print(
                "Run `python -m contracts.generate_schemas` to regenerate.",
                file=sys.stderr,
            )
            return 1
        print("Schemas are in sync.")
        return 0

    written = write_all()
    print(f"Wrote {len(written)} schema(s) to {SCHEMA_DIR}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SCHEMA_DIR",
    "schema_json",
    "schema_filename",
    "write_all",
    "check_all",
    "main",
]
