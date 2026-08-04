"""Stdlib filesystem + env helpers shared across the outbox package.

Single home for the low-level primitives that ``paths``/``store``/``emit`` would
otherwise each re-implement: an env-flag parser, a short-write-safe
``write_all``, an atomic whole-file writer, an ``O_APPEND`` line appender, and the
``.agent-os/.gitignore`` sentinel writer.

**Deliberately stdlib-only, and deliberately NOT imported from ``contracts``.**
``contracts`` carries near-identical copies of ``_write_all`` /
``_ensure_agent_os_gitignore``, but importing *anything* from that package runs
``contracts/__init__.py``, which eagerly imports the pydantic model tree (~300 ms
cold). The outbox producer path (append + emit-context resolution) must stay off
that cost — see card ce79a987 finding 2 (hot-path). So the canonical helpers live
here, in a module that pulls nothing heavy, and ``store``/``paths`` stay
importable without pydantic. Fully unifying these with the ``contracts`` /
``mcp/`` copies would need a shared pydantic-free top-level package (an
architecture change); that consolidation is deferred to a later card.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union

_PathLike = Union[str, Path]

_TRUTHY = {"1", "true", "yes", "on"}


def env_flag(name: str, default: bool = False) -> bool:
    """True iff environment variable ``name`` is set to a truthy token.

    One spelling of the truthy check repo-wide ("1"/"true"/"yes"/"on",
    case/space-insensitive); an unset variable yields ``default``.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def write_all(fd: int, data: bytes) -> None:
    """Write every byte of ``data`` to ``fd``, looping over short writes.

    ``os.write`` may write fewer bytes than requested; a single unchecked call
    is not a guarantee of a complete write. Loop until the buffer is drained
    (mirrors ``contracts.identity._write_all`` — kept here stdlib-only so the
    outbox hot path never imports pydantic).
    """
    view = memoryview(data)
    total = 0
    while total < len(view):
        written = os.write(fd, view[total:])
        if written <= 0:  # pragma: no cover - defensive; POSIX raises instead
            raise OSError("os.write made no progress")
        total += written


def atomic_write_bytes(path: _PathLike, data: bytes, *, fsync: bool = True) -> None:
    """Atomically (re)write ``path`` with ``data``.

    Writes to a uniquely-named temp file in the same directory, ``fsync``s it
    (unless disabled), then ``os.replace``s it into place — so ``path`` only ever
    appears fully-written (same content-atomic posture as ``contracts.identity``).
    The parent directory must already exist.
    """
    target = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    try:
        write_all(fd, data)
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def append_bytes(path: _PathLike, data: bytes, *, fsync: bool = False) -> None:
    """Append ``data`` to ``path`` under ``O_APPEND`` with short-write handling.

    Opens the file ``O_WRONLY | O_CREAT | O_APPEND`` and drains ``data`` with
    :func:`write_all`. On POSIX, ``O_APPEND`` makes each append land at the
    current end of file; a single small write is not interleaved with a
    concurrent writer's append (regular-file append atomicity — see
    ``outbox.store`` for the honest per-platform posture). The write-all loop is a
    correctness net for a rare short write (e.g. an interrupted syscall); callers
    keep lines small so the common case is one uninterrupted write. ``fsync`` is
    off by default (the outbox is a best-effort buffer).
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        write_all(fd, data)
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)


def ensure_gitignore(directory: _PathLike) -> None:
    """Create ``<directory>/.gitignore`` = ``*`` if it does not already exist.

    The wildcard makes git ignore everything under ``.agent-os/`` regardless of
    the project's root ``.gitignore``. Idempotent (an existing file is never
    overwritten) and best-effort (any OS error is swallowed) so it never breaks
    emission. One canonical copy for the outbox package (the ``mcp/server.py`` /
    ``scripts/hook_common.py`` / ``contracts.identity`` copies are noted for a
    later cross-package consolidation card).
    """
    try:
        gitignore_path = Path(directory) / ".gitignore"
        if not gitignore_path.exists():
            gitignore_path.write_text("*\n", encoding="utf-8")
    except OSError:
        pass


__all__ = [
    "env_flag",
    "write_all",
    "atomic_write_bytes",
    "append_bytes",
    "ensure_gitignore",
]
