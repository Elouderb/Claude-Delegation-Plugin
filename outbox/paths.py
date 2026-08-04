"""Filesystem layout for the repository-local sync outbox (proposal §5.4 / §5.5).

Every durable artifact of the local sync plane lives under
``<repo>/.agent-os/sync/`` (proposal §5.4 directory sketch)::

    .agent-os/
      sync/
        outbox.jsonl      # append-only EventEnvelope-per-line producer log
        cursor.json       # {"offset": <byte offset drained so far>}
        dead-letter.jsonl # permanently-undeliverable / malformed lines
        outbox.log        # best-effort diagnostics (swallowed emit failures)

``.agent-os/`` is auto-gitignored: like the card DB and identity files, the first
touch of ``.agent-os/`` here drops a self-protecting ``.agent-os/.gitignore``
(``*``) so the outbox is never committable (mirrors
``mcp/server.py`` / ``scripts/hook_common.py`` / ``contracts.identity``).

Pure stdlib — no third-party imports and nothing from ``mcp/``; the module is
importable identically from ``mcp/`` and ``scripts/`` (both put the repo root on
``sys.path``) and from a future Phase 1c drain worker.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from . import _fsutil

_PathLike = Union[str, Path]

# Subdirectory (relative to the repo root's ``.agent-os/``) that holds the sync
# plane's durable files.
_SYNC_SUBDIR = "sync"

OUTBOX_FILENAME = "outbox.jsonl"
CURSOR_FILENAME = "cursor.json"
DEAD_LETTER_FILENAME = "dead-letter.jsonl"
LOG_FILENAME = "outbox.log"


def find_repo_root(start: Optional[_PathLike] = None) -> Path:
    """Walk upward from ``start`` (or cwd) to the nearest ``.git``; else return it.

    The single stdlib ``.git``-walk for the outbox side (the drain worker uses it
    to locate the repo whose outbox to drain). ``mcp/graph_io.get_repo_root`` is a
    parallel copy that runs inside the MCP server under a different sys.path root;
    consolidating the two into one shared pydantic-free helper is a cross-package
    refactor deferred to a later card (same rationale as ``_fsutil``'s duplicated
    stdlib primitives).
    """
    current = (Path(start) if start is not None else Path.cwd()).resolve()
    search = current
    while search != search.parent:
        if (search / ".git").exists():
            return search
        search = search.parent
    return current


def agent_os_dir(repo_root: _PathLike) -> Path:
    """The repo-local ``.agent-os/`` directory for ``repo_root``."""
    return Path(repo_root) / ".agent-os"


def sync_dir(repo_root: _PathLike) -> Path:
    """The repo-local ``.agent-os/sync/`` directory for ``repo_root``."""
    return agent_os_dir(repo_root) / _SYNC_SUBDIR


def outbox_path(repo_root: _PathLike) -> Path:
    """Path to the append-only ``outbox.jsonl`` for ``repo_root``."""
    return sync_dir(repo_root) / OUTBOX_FILENAME


def cursor_path(repo_root: _PathLike) -> Path:
    """Path to the drain ``cursor.json`` for ``repo_root``."""
    return sync_dir(repo_root) / CURSOR_FILENAME


def dead_letter_path(repo_root: _PathLike) -> Path:
    """Path to the ``dead-letter.jsonl`` for ``repo_root``."""
    return sync_dir(repo_root) / DEAD_LETTER_FILENAME


def log_path(repo_root: _PathLike) -> Path:
    """Path to the best-effort ``outbox.log`` for ``repo_root``."""
    return sync_dir(repo_root) / LOG_FILENAME


def ensure_sync_dir(repo_root: _PathLike) -> Path:
    """Create ``.agent-os/sync/`` (and the protecting sentinel) and return it.

    Ensures the ``.agent-os/`` sentinel first (via the canonical
    :func:`outbox._fsutil.ensure_gitignore`) so a freshly-created outbox
    directory can never be committed, then creates ``sync/``.
    """
    base = agent_os_dir(repo_root)
    base.mkdir(parents=True, exist_ok=True)
    _fsutil.ensure_gitignore(base)
    target = base / _SYNC_SUBDIR
    target.mkdir(parents=True, exist_ok=True)
    return target


__all__ = [
    "OUTBOX_FILENAME",
    "CURSOR_FILENAME",
    "DEAD_LETTER_FILENAME",
    "LOG_FILENAME",
    "find_repo_root",
    "agent_os_dir",
    "sync_dir",
    "outbox_path",
    "cursor_path",
    "dead_letter_path",
    "log_path",
    "ensure_sync_dir",
]
