"""Dependency detection, SQLite backend resolution, and paths for memory.

The memory subsystem leans on three optional pieces:

  * ``sqlite-vec``            — the vec0 vector-search SQLite extension.
  * a loadable-extension-capable SQLite module — needed to actually load that
    extension.  Many Python builds (pyenv, some distro/macOS builds) ship a
    stdlib ``sqlite3`` compiled WITHOUT ``--enable-loadable-sqlite-extensions``
    (``enable_load_extension`` is absent or disabled).  ``pysqlite3-binary``
    bundles a modern statically-linked SQLite with extension loading enabled and
    is the project-recommended way to get sqlite-vec working portably, so it is
    the preferred backend; the stdlib module is used only when it actually loads
    extensions.
  * ``sentence-transformers`` — the embedding model runtime (pulls torch).

Nothing in this module imports the *heavy* dependency (sentence-transformers /
torch) — presence is probed with :func:`importlib.util.find_spec`, which never
executes the module — so ``check_availability`` stays cheap and cannot be
brought down by a broken torch install.  Importing ``memory.availability`` is
always safe.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
from typing import Optional

# Canonical Phase-0 embedding model (bge-small-en-v1.5, 384-dim).  Stored per
# chunk row so a future model migration can tell old vectors apart.
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_EMBEDDING_DIM = 384

# The retrieval instruction bge-* v1.5 recommends prepending to *queries* only
# (passages are embedded verbatim).
BGE_QUERY_PROMPT = "Represent this sentence for searching relevant passages: "

# Environment override so tests never touch the real ~/.agent-os.  When set, the
# central store lives at ``$AGENT_OS_MEMORY_HOME/memory.sqlite``.
_HOME_ENV = "AGENT_OS_MEMORY_HOME"

# The single canonical install-hint string (referenced by the store, embedder,
# and tool layers rather than re-spelled in each).
_INSTALL_HINT = (
    "memory subsystem unavailable — install its optional dependencies with "
    "`pip install -r mcp/memory_requirements.txt`"
)


class MemoryUnavailable(RuntimeError):
    """Raised when a memory operation needs an absent optional dependency."""


def default_db_path() -> Path:
    """Resolve the central memory database path.

    ``$AGENT_OS_MEMORY_HOME/memory.sqlite`` when the override is set (tests),
    otherwise ``~/.agent-os/central-memory/memory.sqlite``.  ``~`` and env vars
    in the override are expanded so a value like ``~/tmp`` never creates a
    literal ``./~`` directory.  This store is deliberately machine-global, NOT
    repo-local.
    """
    override = os.environ.get(_HOME_ENV)
    if override:
        root = Path(os.path.expanduser(os.path.expandvars(override)))
    else:
        root = Path.home() / ".agent-os" / "central-memory"
    return root / "memory.sqlite"


def _module_can_load_extensions(mod) -> bool:
    """Return True if a sqlite module can actually load runtime extensions.

    ``enable_load_extension`` only exists — and only succeeds — when SQLite was
    compiled with loadable-extension support, so exercise it on a throwaway
    in-memory connection rather than trusting the attribute's mere presence (it
    can exist but raise when the feature is disabled at build time).
    """
    try:
        conn = mod.connect(":memory:")
    except Exception:
        return False
    try:
        conn.enable_load_extension(True)
        conn.enable_load_extension(False)
        return True
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def resolve_sqlite_module():
    """Return an (module, name) pair for a loadable-extension-capable SQLite.

    Prefers ``pysqlite3`` (bundled modern SQLite), then falls back to the stdlib
    ``sqlite3`` only if that build can load extensions.  Raises
    :class:`MemoryUnavailable` when neither can.
    """
    for name in ("pysqlite3", "sqlite3"):
        try:
            mod = importlib.import_module(name)
        except ImportError:
            continue
        if _module_can_load_extensions(mod):
            return mod, name
    raise MemoryUnavailable(
        "no loadable-extension-capable SQLite module found "
        "(install pysqlite3-binary, or use a Python built with "
        "--enable-loadable-sqlite-extensions)"
    )


def _has_module(name: str) -> bool:
    """Report whether ``name`` is importable WITHOUT importing it (no side effects).

    Uses ``find_spec`` so probing for ``sentence_transformers`` never actually
    loads it (or torch) — that keeps ``memory_status`` fast and immune to a
    broken/partly-installed heavy dependency crashing at import time.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def check_availability() -> dict:
    """Report which memory capabilities are present, without importing heavies.

    Returns a dict describing storage vs. embedder readiness.  ``available`` is
    True only when both the storage stack (sqlite-vec + a capable SQLite) and
    the embedder (sentence-transformers) are usable — that is what
    ``memory_ingest`` / ``memory_query`` require.  ``memory_status`` uses the
    finer-grained fields to explain exactly what is missing.
    """
    missing = []

    has_sqlite_vec = _has_module("sqlite_vec")
    if not has_sqlite_vec:
        missing.append("sqlite-vec")

    sqlite_backend: Optional[str] = None
    try:
        _, sqlite_backend = resolve_sqlite_module()
    except MemoryUnavailable:
        missing.append("loadable-extension-capable SQLite (pysqlite3-binary)")

    storage_available = has_sqlite_vec and sqlite_backend is not None

    embedder_available = _has_module("sentence_transformers")
    if not embedder_available:
        missing.append("sentence-transformers")

    return {
        "available": storage_available and embedder_available,
        "storage_available": storage_available,
        "embedder_available": embedder_available,
        "sqlite_backend": sqlite_backend,
        "missing": missing,
        "hint": _INSTALL_HINT,
    }
