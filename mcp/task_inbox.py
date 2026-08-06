"""Task down-projection poller: pull central tasks and apply them locally (Phase 2a).

The DOWN half of task synchronization — the mirror of :mod:`outbox.drain` (the UP
half). Where the drain ships this repo's card events UP to the central ``/v1`` API,
this module pulls central task projections DOWN and applies them into the local
``cards.sqlite`` via :func:`card_tools.apply_task_projection`. Together they close
the loop the plugin was missing: a card created on one machine becomes a task in a
SECOND machine's local card DB.

Cursor discipline (the correctness core)
-----------------------------------------
Per source repository the poller keeps a keyset cursor — the largest ``change_seq``
(``registry.tasks``' ROWVERSION-derived total order) it has durably applied —
persisted atomically under ``<repo>/.agent-os/sync/inbox_cursor.json`` (a dict keyed
by source ``repository_id``, so one local repo can pull from several sources). Each
poll:

* reads the stored ``since`` for that source and requests ``GET /v1/tasks``;
* applies EVERY task on the page via ``apply_fn`` (idempotent, keyed by
  ``global_id`` — a re-apply updates the same local card in place and NEVER re-emits
  a task event, so there is no sync loop);
* advances the cursor to the page's ``next_since`` ONLY after every task on the page
  applied cleanly. A network outage, a partial page, or an apply error leaves the
  cursor where it was — the next poll re-pulls and re-applies, and because apply is
  idempotent that costs a repeat, never a corrupted or duplicated local card.

Transport is the stdlib-only :class:`services.m3_client.http.Client` (urllib; no
third-party ``requests``), so the poller adds no dependency and stays Windows-safe.
Applying is delegated to ``card_tools`` (lazily imported) so this module has no hard
import-time dependency on the MCP package.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from outbox import _fsutil, paths

_PathLike = Union[str, Path]

INBOX_CURSOR_FILENAME = "inbox_cursor.json"

# An apply function takes one central task dict and returns the applied local card
# dict (or an ``{"error": ...}`` dict on failure — apply never raises).
ApplyFn = Callable[[Dict[str, Any]], Dict[str, Any]]


def _inbox_cursor_path(repo_root: _PathLike) -> Path:
    """Path to the down-projection keyset cursor for ``repo_root``."""
    return paths.sync_dir(repo_root) / INBOX_CURSOR_FILENAME


def read_inbox_cursors(repo_root: _PathLike) -> Dict[str, int]:
    """Return the ``{source_repository_id: last_applied_change_seq}`` cursor map.

    Defensive: a missing, unreadable, or non-object cursor file yields ``{}`` (a
    fresh start), and any non-integer value is dropped — never raising, so a
    corrupt cursor degrades to a full re-pull (safe, because apply is idempotent).
    """
    try:
        data = json.loads(_inbox_cursor_path(repo_root).read_bytes())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    cursors: Dict[str, int] = {}
    for key, value in data.items():
        try:
            cursors[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return cursors


def read_inbox_cursor(repo_root: _PathLike, repository_id: str) -> Optional[int]:
    """Return the last-applied ``change_seq`` for one source repo, or ``None``."""
    return read_inbox_cursors(repo_root).get(repository_id)


def write_inbox_cursor(repo_root: _PathLike, repository_id: str, since: int) -> None:
    """Atomically persist the keyset cursor for one source repository.

    Reads the current map, updates one key, and rewrites the whole file with the
    shared atomic writer (temp file + ``fsync`` + ``os.replace``) so the cursor
    file only ever appears fully-written.
    """
    cursors = read_inbox_cursors(repo_root)
    cursors[str(repository_id)] = int(since)
    paths.ensure_sync_dir(repo_root)
    payload = json.dumps(cursors).encode("utf-8")
    _fsutil.atomic_write_bytes(_inbox_cursor_path(repo_root), payload, fsync=True)


def _resolve_apply_fn(apply_fn: Optional[ApplyFn]) -> ApplyFn:
    """Return ``apply_fn`` or lazily bind ``card_tools.apply_task_projection``.

    Lazy so importing this module never forces the MCP ``card_tools`` import; a
    caller (or test) may inject its own apply function.
    """
    if apply_fn is not None:
        return apply_fn
    import card_tools  # lazy: keeps this module importable without the MCP package

    return card_tools.apply_task_projection


def apply_page(tasks: List[Dict[str, Any]], apply_fn: ApplyFn) -> int:
    """Apply every task on one page; raise on the first apply that reports an error.

    Returns the number applied. Raising on the first ``{"error": ...}`` (or missing
    ``global_id``) is deliberate: the caller must NOT advance the cursor past a page
    it could not fully apply, so a failed apply holds the cursor exactly like the
    drain's transport failure holds its byte offset.
    """
    applied = 0
    for task in tasks:
        result = apply_fn(task)
        if not isinstance(result, dict) or "error" in result:
            raise RuntimeError(
                f"apply_task_projection failed for global_id="
                f"{(task or {}).get('global_id')!r}: {result}"
            )
        applied += 1
    return applied


def poll_once(
    client: Any,
    repo_root: _PathLike,
    source_repository_id: str,
    *,
    apply_fn: Optional[ApplyFn] = None,
    limit: int = 200,
    max_pages: int = 1000,
) -> int:
    """Pull + apply all pending tasks for one source repository. Returns count applied.

    Keyset-walks ``GET /v1/tasks?repository_id=&since=`` from the persisted cursor,
    applying each page and advancing the cursor to ``next_since`` only after the
    whole page applied cleanly. Stops when the source is drained (empty page /
    ``next_since`` null), when the cursor stops advancing (defensive against a
    server that never returns a continuation), or after ``max_pages`` (a hard bound
    against an unexpected loop). Propagates any apply/transport error WITHOUT
    advancing the cursor — the next poll safely re-pulls (apply is idempotent).
    """
    resolved_apply = _resolve_apply_fn(apply_fn)
    since = read_inbox_cursor(repo_root, source_repository_id)
    applied_total = 0
    for _ in range(max_pages):
        body = client.get_tasks(
            repository_id=source_repository_id, since=since, limit=limit
        )
        tasks = body.get("tasks") or []
        if not tasks:
            break
        applied_total += apply_page(tasks, resolved_apply)
        next_since = body.get("next_since")
        if next_since is None or (since is not None and int(next_since) <= int(since)):
            # Nothing further advanced the cursor — apply what we have and stop.
            if next_since is not None:
                write_inbox_cursor(repo_root, source_repository_id, int(next_since))
                since = int(next_since)
            break
        write_inbox_cursor(repo_root, source_repository_id, int(next_since))
        since = int(next_since)
        if len(tasks) < limit:
            break  # last (partial) page fully applied; source is drained
    return applied_total


__all__ = [
    "INBOX_CURSOR_FILENAME",
    "read_inbox_cursors",
    "read_inbox_cursor",
    "write_inbox_cursor",
    "apply_page",
    "poll_once",
]
