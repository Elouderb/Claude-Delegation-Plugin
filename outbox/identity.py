"""Plugin-side identity bootstrap glue (proposal §22.3 / §5.4).

Phase 0 shipped the *mechanism* (``contracts.identity.ensure_machine_identity`` /
``ensure_repository_identity`` — already atomic, race-safe, self-healing). Phase
1b is where the living plugin actually *calls* it: once on MCP-server startup and
once on the hook entry path, so every machine and every checkout has a stable
``mach_`` / ``repo_`` id that later emission and sync layers stamp onto events.

:func:`bootstrap_identity` is the single shared entry both call sites use. It is
cheap and idempotent by design, and specifically **hot-path aware**: the
``canonical_remote`` fingerprint requires ``git remote get-url origin`` (a local
git-config read — *not* a network call), which is only worth paying on the *first*
run for a checkout. Once the repo identity file exists, we take the fast path and
skip the subprocess entirely (the value is stamped only at creation and never
mutated afterwards, per ``contracts.identity``), so steady-state bootstrap is two
small file reads. No-git / no-remote checkouts are tolerated: the git probe is
guarded and simply yields ``None``.

stdlib + ``contracts`` only; never imports ``mcp/``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple, Union

from . import emit

if TYPE_CHECKING:  # type-only: no runtime dependency beyond the lazy import below
    from contracts.identity import MachineIdentity, RepositoryIdentity

_PathLike = Union[str, Path]

# git remote get-url reads local .git/config; a short timeout keeps the one
# first-run subprocess from ever hanging a startup or a hook.
_GIT_TIMEOUT = 5

# Machine-global home override (mirrors contracts.identity._HOME_ENV). Duplicated
# here — with the two path derivations below — deliberately: the steady-state
# fast path must resolve the identity file locations with the STANDARD LIBRARY
# ONLY, because importing contracts.identity to reuse machine_home() would pull
# the whole pydantic model tree (~300 ms cold) onto the hook hot path (card
# ce79a987 finding 2). These mirror contracts.identity.machine_home /
# machine_identity_path / repository_identity_path exactly.
_HOME_ENV = "AGENT_OS_HOME"


def _machine_home() -> Path:
    override = os.environ.get(_HOME_ENV)
    if override:
        return Path(os.path.expanduser(os.path.expandvars(override)))
    return Path.home() / ".agent-os"


def _machine_identity_path() -> Path:
    return _machine_home() / "identity.json"


def _repository_identity_path(repo_root: _PathLike) -> Path:
    return Path(repo_root) / ".agent-os" / "identity.json"


def _read_context_stdlib(
    machine_file: Path, repo_file: Path
) -> Optional[Tuple[str, str, Optional[str]]]:
    """Read ``(machine_id, repository_id, project_id)`` with the stdlib only.

    Parses the two identity JSON files without importing ``contracts``/pydantic.
    Returns ``None`` if either file is missing, unreadable, non-object, or lacks a
    string id — the caller then falls back to the full, self-healing contracts
    bootstrap (which revalidates/repairs). The files were written+validated by
    ``contracts.identity`` when created, so a plain read is sufficient on the
    steady-state fast path.
    """
    try:
        machine = json.loads(machine_file.read_text(encoding="utf-8"))
        repo = json.loads(repo_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(machine, dict) or not isinstance(repo, dict):
        return None
    machine_id = machine.get("machine_id")
    repository_id = repo.get("repository_id")
    if not isinstance(machine_id, str) or not isinstance(repository_id, str):
        return None
    project_id = repo.get("project_id")
    if project_id is not None and not isinstance(project_id, str):
        project_id = None
    return machine_id, repository_id, project_id


def git_remote_origin(repo_root: _PathLike) -> Optional[str]:
    """Return the ``origin`` remote URL for ``repo_root``, or ``None``.

    Uses ``git remote get-url origin`` — a purely local git-config read, no
    network. Any failure (not a git repo, no ``origin`` remote, git absent,
    timeout) is swallowed and yields ``None`` so a no-git / no-remote checkout is
    tolerated gracefully.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            # stdin=DEVNULL: this runs in the MCP-server process at startup
            # (bootstrap_identity -> here, before server.run()); without it the git
            # child inherits the server's blocking-read stdin pipe and can stall on
            # Windows (the M2 hang class), delaying run() and taking every tool down.
            # git remote get-url never reads stdin, so DEVNULL is purely defensive.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def bootstrap_identity(
    repo_root: _PathLike,
) -> "Optional[Tuple[MachineIdentity, RepositoryIdentity]]":
    """Idempotently ensure this machine's and this repo's identity files exist.

    Called once on MCP-server startup and once per hook entry (gated to lifecycle
    reasons in ``scripts/sync_repo_graph.py`` so the true hot path never pays it).

    **Steady-state fast path (no pydantic).** When both identity files already
    exist, their ids are read with the standard library only — NO
    ``contracts``/pydantic import — the :func:`emit.seed_context` cache is seeded
    so the immediately-following emit skips its own read+validate, and ``None`` is
    returned (the pydantic models are intentionally not constructed on this path).
    This is what keeps a lifecycle hook / server startup off the ~300 ms cold
    pydantic import once identity exists (card ce79a987 finding 2).

    **First-run path.** If either file is absent (or the stdlib read fails), the
    full self-healing ``contracts.identity`` bootstrap runs: it mints ids, stamps
    the ``canonical_remote`` fingerprint (``git remote get-url origin`` — a local
    config read, first-run only) and the creating ``machine_id`` on first
    creation, seeds the emit cache, and returns ``(MachineIdentity,
    RepositoryIdentity)``. No-git / no-remote checkouts are tolerated.
    """
    root = Path(repo_root)
    machine_file = _machine_identity_path()
    repo_file = _repository_identity_path(root)
    if machine_file.exists() and repo_file.exists():
        ids = _read_context_stdlib(machine_file, repo_file)
        if ids is not None:
            emit.seed_context(root, ids)
            return None

    # First run (or an unreadable/partial fast-path file): full contracts path.
    from contracts.identity import (
        ensure_machine_identity,
        ensure_repository_identity,
        repository_identity_path,
    )

    machine = ensure_machine_identity()
    if repository_identity_path(root).exists():
        # Repo identity already minted — no git subprocess, no re-stamping.
        repo = ensure_repository_identity(root)
    else:
        remote = git_remote_origin(root)
        repo = ensure_repository_identity(
            root,
            canonical_remote=remote,
            machine_id=machine.machine_id,
        )
    emit.seed_context(root, (machine.machine_id, repo.repository_id, repo.project_id))
    return machine, repo


__all__ = ["git_remote_origin", "bootstrap_identity"]
