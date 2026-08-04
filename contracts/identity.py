"""Machine and repository identity (ruling 4 / proposal §22.3, §5.4).

Two identity files anchor everything else:

  * **machine-global** ``~/.agent-os/identity.json`` — one stable ``mach_`` id per
    machine, plus a human name and OS;
  * **repository-local** ``<repo>/.agent-os/identity.json`` — one stable ``repo_``
    id (and optional ``proj_`` id, ``canonical_remote`` fingerprint, and creating
    ``machine_id``) per checkout.  The repo file lives under ``.agent-os/``, which
    the plugin auto-gitignores; this module writes the self-protecting
    ``.agent-os/.gitignore`` (``*``) sentinel itself when creating the directory
    (mirroring the card-DB init in ``mcp/server.py``) so a fresh clone can never
    commit ``identity.json`` and later have a ``git checkout``/``reset`` revert it
    to a stale ``repository_id`` — the 0.2.7 git-driven-loss class.

Creation is **idempotent** and **safe under concurrent first-run**, and — new in
the review fix — **content-atomic** and **self-healing**.  A second call returns
the same ids.  Racing first-run processes resolve to a single winner: each writes
its candidate to a uniquely-named temp file in the same directory, ``fsync``s it,
then *claims* the final path with an exclusive ``os.link`` (a lock-file +
``os.replace`` fallback covers filesystems without hard links).  Because the
final path only ever appears as a fully-written file, a reader never observes a
partial write.  If a winner is killed between claim and completion, the leftover
final file is invalid; once it is *stale* (mtime older than a few seconds) it is
treated as reclaimable — deleted and re-claimed — so a dead winner cannot
permanently brick machine identity.

Everything here is pure and local: no network, stdlib + pydantic only.  The
machine home honors the ``AGENT_OS_HOME`` environment override (default
``~/.agent-os``), mirroring the ``AGENT_OS_MEMORY_HOME`` precedent so tests never
touch a real home.
"""

from __future__ import annotations

import os
import platform
import re
import socket
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional, Type, TypeVar

from pydantic import AwareDatetime, Field, ValidationError

from .base import AgentOSModel, utc_now
from .identifiers import MACHINE, PROJECT, REPOSITORY, new_id

# Environment override for the machine-global Agent OS home.  When set, the
# machine identity lives at ``$AGENT_OS_HOME/identity.json``; otherwise at
# ``~/.agent-os/identity.json``.  Tests point this at a tmp dir.
_HOME_ENV = "AGENT_OS_HOME"

# Race/self-heal tuning.  The claim budget is deliberately *shorter* than the
# stale threshold: within the budget a not-yet-readable final file is assumed to
# be a live winner mid-write and is retried with backoff; a genuinely corrupt
# *fresh* file therefore surfaces as an error (it never ages past the budget),
# while a file left behind by a *killed* winner ages past the stale threshold and
# is reclaimed.  Both are seconds-scale so a descheduled winner on a loaded box is
# not spuriously failed.
_CLAIM_BUDGET = 2.0  # seconds to wait for a live winner before surfacing an error
_STALE_SECONDS = 5.0  # an invalid final file older than this is reclaimable
_INITIAL_DELAY = 0.01
_MAX_DELAY = 0.2
_MAX_RECLAIMS = 3

_T = TypeVar("_T", bound=AgentOSModel)


class MachineIdentity(AgentOSModel):
    """Stable identity of a single machine (ruling 4)."""

    machine_id: str = Field(description="Prefixed ULID for the machine (mach_...).")
    name: str = Field(description="Human-friendly machine name (e.g. hostname).")
    os: str = Field(description="Operating system, e.g. 'Linux' / 'Windows'.")
    created_at: AwareDatetime


class RepositoryIdentity(AgentOSModel):
    """Stable identity of a single repository checkout (ruling 4).

    ``canonical_remote`` is the normalized origin-remote fingerprint (see
    :func:`canonical_remote_url`): two checkouts of the *same* logical repository
    on different machines share this value, which a later sync layer uses to
    recognise them as the same repo instead of partitioning tasks per checkout.
    ``machine_id`` records which machine minted this checkout's identity.  Both
    are optional — Phase 0 defines the shape; a caller supplies them when known.
    """

    repository_id: str = Field(description="Prefixed ULID for the repo (repo_...).")
    project_id: Optional[str] = Field(
        default=None,
        description="Owning project id (proj_...); None until a project is assigned.",
    )
    canonical_remote: Optional[str] = Field(
        default=None,
        description=(
            "Normalized origin-remote fingerprint (host/owner/repo) correlating "
            "the same logical repo across machines; None if no remote is known."
        ),
    )
    machine_id: Optional[str] = Field(
        default=None,
        description="Machine id (mach_...) that created this checkout's identity.",
    )
    created_at: AwareDatetime


# -- remote-URL normalization --------------------------------------------------
# scp-style remote: ``user@host:owner/repo`` (no scheme).  Captured as (host, path).
_SCP_REMOTE = re.compile(r"^[^/@:]+@([^:/]+):(.+)$")


def canonical_remote_url(remote: str) -> str:
    """Normalize a git remote URL into a stable cross-checkout fingerprint.

    Two checkouts of the same logical repository — cloned on different machines,
    over https or ssh, with or without a trailing ``.git``, with or without
    embedded credentials — normalize to the *same* string, so a later sync layer
    can recognise them as the same repo rather than partitioning tasks per
    checkout.

    Steps: strip whitespace and lowercase; convert scp-style
    ``git@host:owner/repo`` to ``host/owner/repo``; otherwise drop the scheme
    (``https://`` / ``ssh://`` / ``git://``) and any ``user[:pass]@`` credentials;
    strip a trailing slash and a trailing ``.git``.  An empty/whitespace input
    normalizes to ``""``.
    """
    s = remote.strip().lower()
    if not s:
        return ""
    scp = _SCP_REMOTE.match(s)
    if scp and "://" not in s:
        s = f"{scp.group(1)}/{scp.group(2)}"
    else:
        if "://" in s:
            s = s.split("://", 1)[1]
        if "@" in s:
            s = s.split("@", 1)[1]
    s = s.rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    return s


# -- path resolution -----------------------------------------------------------
def machine_home() -> Path:
    """Resolve the machine-global Agent OS home directory.

    ``$AGENT_OS_HOME`` when set (``~`` and env vars expanded so ``~/tmp`` never
    creates a literal ``./~``), otherwise ``~/.agent-os``.
    """
    override = os.environ.get(_HOME_ENV)
    if override:
        return Path(os.path.expanduser(os.path.expandvars(override)))
    return Path.home() / ".agent-os"


def machine_identity_path() -> Path:
    """Path to the machine-global ``identity.json``."""
    return machine_home() / "identity.json"


def repository_identity_path(repo_root: Path) -> Path:
    """Path to a repository's ``.agent-os/identity.json`` given its root."""
    return Path(repo_root) / ".agent-os" / "identity.json"


def _ensure_agent_os_gitignore(agent_os_dir: Path) -> None:
    """Create ``<agent_os_dir>/.gitignore`` = ``*`` if it does not already exist.

    Mirrors ``mcp/server.py._ensure_agent_os_gitignore`` exactly: the wildcard
    makes git ignore everything under ``.agent-os/`` regardless of the project's
    root ``.gitignore``.  Idempotent (an existing file is never overwritten) and
    best-effort (any OS error is swallowed so identity creation never breaks).
    """
    try:
        gitignore_path = agent_os_dir / ".gitignore"
        if not gitignore_path.exists():
            gitignore_path.write_text("*\n", encoding="utf-8")
    except OSError:
        pass


# -- idempotent, race-safe, content-atomic, self-healing creation --------------
def _try_read(path: Path, model_cls: Type[_T]) -> Optional[_T]:
    """Return the parsed identity if present and valid, else ``None``.

    An absent, empty, or currently-partial file yields ``None`` (the caller will
    either create it or retry), so this never raises on a benign race.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    if not text.strip():
        return None
    try:
        return model_cls.model_validate_json(text)
    except ValidationError:
        return None


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of ``data`` to ``fd``, looping over short writes.

    ``os.write`` may write fewer bytes than requested; a single unchecked call is
    not a guarantee of a complete write.  Loop until the buffer is drained.
    """
    view = memoryview(data)
    total = 0
    while total < len(view):
        written = os.write(fd, view[total:])
        if written <= 0:  # pragma: no cover - defensive; POSIX raises instead
            raise OSError("os.write made no progress")
        total += written


def _claim_via_lock(path: Path, tmp: str) -> bool:
    """Fallback claim for filesystems without hard-link support.

    Take an exclusive lock file, and — only if the final path is still absent —
    ``os.replace`` the already-written, fsynced temp file into place (an atomic
    rename within the directory).  Returns ``True`` iff this call placed the file.
    """
    lock = path.with_name(path.name + ".lock")
    try:
        lock_fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        if path.exists():
            return False
        os.replace(tmp, path)
        return True
    finally:
        os.close(lock_fd)
        try:
            os.unlink(lock)
        except OSError:
            pass


def _atomic_claim(path: Path, payload: str) -> bool:
    """Claim ``path`` with ``payload``, exclusively and content-atomically.

    Write the full payload to a uniquely-named temp file in the same directory,
    ``fsync`` it, then claim the final name with ``os.link`` (which fails with
    ``FileExistsError`` if another process already claimed it).  The final path
    only ever appears as a complete file, so a reader never sees a partial write.
    Returns ``True`` iff this call created the final file.
    """
    directory = str(path.parent)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=path.name + ".", suffix=".tmp")
    try:
        _write_all(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        try:
            os.link(tmp, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            # Hard links unsupported here (e.g. some Windows/FAT volumes) — fall
            # back to a lock-file + atomic-rename claim.
            return _claim_via_lock(path, tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _is_stale(path: Path) -> bool:
    """True iff ``path`` exists and its mtime is older than the stale threshold."""
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age > _STALE_SECONDS


def _reclaim(path: Path) -> None:
    """Best-effort delete of a stale, invalid final file so it can be re-claimed."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _ensure_identity(
    path: Path, model_cls: Type[_T], build: Callable[[], _T]
) -> _T:
    """Idempotently create-or-read an identity file at ``path``.

    Fast path: an existing valid file is returned unchanged (same ids on every
    call).  Otherwise the candidate is built once and claimed content-atomically;
    a process that loses the race reads the winner's completed file.  A final file
    left behind (empty/corrupt) by a *killed* winner self-heals: once stale it is
    reclaimed and the claim retried.  A genuinely corrupt *fresh* file surfaces as
    a ``RuntimeError`` after the (shorter-than-stale) claim budget.
    """
    existing = _try_read(path, model_cls)
    if existing is not None:
        return existing

    path.parent.mkdir(parents=True, exist_ok=True)

    model: Optional[_T] = None
    payload: Optional[str] = None
    reclaims_left = _MAX_RECLAIMS
    deadline = time.monotonic() + _CLAIM_BUDGET
    delay = _INITIAL_DELAY

    while True:
        # A concurrent winner may have finished; prefer reading a valid file.
        existing = _try_read(path, model_cls)
        if existing is not None:
            return existing

        # Build our candidate once — a new id is minted only if we go on to win.
        if model is None:
            model = build()
            payload = model.model_dump_json(indent=2) + "\n"
        assert payload is not None

        if _atomic_claim(path, payload):
            return model

        # Lost the claim, or a file already exists at the path. Try to read it.
        parsed = _try_read(path, model_cls)
        if parsed is not None:
            return parsed

        # A file exists but is empty/corrupt. If it is stale, a previous winner
        # was killed mid-flight — reclaim it and retry (bounded). Otherwise a live
        # winner is mid-write; back off within the budget, then surface an error.
        if _is_stale(path):
            if reclaims_left <= 0:
                raise RuntimeError(
                    f"identity file persistently unreadable at {path}"
                )
            reclaims_left -= 1
            _reclaim(path)
            continue

        if time.monotonic() >= deadline:
            raise RuntimeError(f"identity file present but unreadable: {path}")
        time.sleep(delay)
        delay = min(delay * 2, _MAX_DELAY)


def ensure_machine_identity(
    name: Optional[str] = None, os_name: Optional[str] = None
) -> MachineIdentity:
    """Create (idempotently) and return this machine's identity.

    ``name`` defaults to the hostname and ``os_name`` to ``platform.system()``.
    The file is written under :func:`machine_home` (``AGENT_OS_HOME`` override).
    """

    def build() -> MachineIdentity:
        return MachineIdentity(
            machine_id=new_id(MACHINE),
            name=name or socket.gethostname(),
            os=os_name or platform.system(),
            created_at=utc_now(),
        )

    return _ensure_identity(machine_identity_path(), MachineIdentity, build)


def ensure_repository_identity(
    repo_root: Path,
    project_id: Optional[str] = None,
    canonical_remote: Optional[str] = None,
    machine_id: Optional[str] = None,
) -> RepositoryIdentity:
    """Create (idempotently) and return the identity for the repo at ``repo_root``.

    Written to ``<repo_root>/.agent-os/identity.json`` (auto-gitignored — this
    function writes the ``.agent-os/.gitignore`` = ``*`` sentinel when creating
    the directory, so the identity file is never committable).  ``project_id``,
    ``canonical_remote`` (normalized via :func:`canonical_remote_url`), and
    ``machine_id`` are only stamped on first creation; a later call with
    different values does not mutate an existing file (identity is stable).
    """
    path = repository_identity_path(repo_root)
    # Ensure the repo-local .agent-os/ exists and is self-protected BEFORE the
    # identity file can be created, so identity.json is never committable.
    path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_agent_os_gitignore(path.parent)

    normalized_remote = (
        canonical_remote_url(canonical_remote) if canonical_remote else None
    )

    def build() -> RepositoryIdentity:
        return RepositoryIdentity(
            repository_id=new_id(REPOSITORY),
            project_id=project_id if project_id else None,
            canonical_remote=normalized_remote,
            machine_id=machine_id if machine_id else None,
            created_at=utc_now(),
        )

    return _ensure_identity(path, RepositoryIdentity, build)


def new_project_id() -> str:
    """Mint a fresh project id (``proj_...``).

    Projects are assigned out-of-band (a repo may join an existing project), so
    there is no project *file*; this is the id-minting helper for that flow.
    """
    return new_id(PROJECT)


__all__ = [
    "MachineIdentity",
    "RepositoryIdentity",
    "canonical_remote_url",
    "machine_home",
    "machine_identity_path",
    "repository_identity_path",
    "ensure_machine_identity",
    "ensure_repository_identity",
    "new_project_id",
]
