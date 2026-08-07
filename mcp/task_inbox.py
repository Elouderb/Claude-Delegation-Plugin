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

CLI (card dadf614c)
--------------------
``poll_once`` is a library function; this module also runs operationally as
``python mcp/task_inbox.py [--once]`` — the down-path mirror of
``python -m outbox.drain [--once]``. It is invoked as a **direct script path**,
not ``python -m mcp.task_inbox``: the repo's ``mcp/`` directory collides with the
pip-installed ``mcp`` SDK package (the same reason ``mcp/server.py`` is always
launched as ``<python> mcp/server.py`` in ``templates/mcp.json.tmpl``, never via
``-m``) — ``python -m mcp.task_inbox`` resolves ``mcp`` to the SDK package first
and fails with ``No module named 'mcp.task_inbox'`` regardless of ``sys.path``
order, since a regular (``__init__.py``-bearing) package always wins over a
namespace-package directory of the same name. See :func:`main`.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

# Run as a direct script (``python mcp/task_inbox.py``), sys.path[0] is already
# ``mcp/`` (bare imports of sibling modules like ``card_tools`` resolve), but the
# REPO ROOT is not yet on sys.path — needed for the top-level ``outbox`` and
# ``services`` packages below. Mirrors ``mcp/server.py``'s bootstrap exactly.
# A no-op when already imported through the running server (repo root already
# on sys.path there) or through a test that manages sys.path itself.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

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


# --------------------------------------------------------------------------- #
# CLI (card dadf614c): a runnable wrapper around poll_once. No poll/apply logic
# lives here — only argument/env resolution, local-DB bootstrap, logging, and
# loop/shutdown plumbing, mirroring outbox/drain.py's main()/Drainer shape.
# --------------------------------------------------------------------------- #

# Env var names matched EXACTLY to outbox/drain.py so M2 config is symmetric
# between the up-path (drain) and down-path (this module).
_URL_ENV = "AGENT_OS_API_URL"
_KEY_ENV = "AGENT_OS_API_KEY"
# task-inbox-specific: which source repository's tasks to pull. No drain.py
# analogue (the up-path has no "source" concept), so this is a new name rather
# than a borrowed one.
_SOURCE_ENV = "AGENT_OS_SOURCE_REPOSITORY_ID"

_DEFAULT_URL = "http://127.0.0.1:8765"
_DEFAULT_INTERVAL = 30.0
_DEFAULT_TIMEOUT = 10.0


def _log(message: str) -> None:
    """Timestamped stderr line. A standalone copy of ``outbox.drain._log``'s
    pattern rather than an import from it: importing ``outbox.drain`` pulls in
    the heavier ``contracts`` (pydantic) chain that this module's docstring
    says it otherwise avoids at import time. Best-effort — never raises."""
    try:
        stamp = datetime.now(timezone.utc).isoformat()
        print(f"{stamp} [task-inbox] {message}", file=sys.stderr, flush=True)
    except Exception:  # pragma: no cover - stderr closed / encoding edge
        pass


def _bootstrap_local_card_db(repo_root: Path) -> None:
    """Wire ``card_tools``/``graph_io`` at the standard ``.agent-os`` path so
    ``poll_once``'s default ``apply_fn`` (``card_tools.apply_task_projection``,
    resolved lazily by :func:`_resolve_apply_fn`) writes into THIS repo's local
    card DB — mirrors ``mcp/server.py``'s ``ensure_agent_os()`` startup bootstrap
    exactly. Lazy import (only reached from the CLI path in :func:`main`): a
    caller that imports this module as a library and injects its own
    ``apply_fn`` never pays for or requires the MCP package.
    """
    import card_tools
    import graph_io

    agent_os_dir = Path(repo_root) / ".agent-os"
    agent_os_dir.mkdir(parents=True, exist_ok=True)
    card_tools.set_db_path(agent_os_dir / "cards.sqlite")
    card_tools.init_db()
    graph_io.set_emission_repo_root(repo_root)


def _logging_apply_fn(base_apply_fn: ApplyFn) -> ApplyFn:
    """Wrap ``base_apply_fn`` to log each successfully-applied task's title.

    Purely observational: the wrapped call's return value passes straight
    through unchanged, so this changes no poll/apply semantics — it uses the
    ``apply_fn`` extension point ``poll_once`` already exposes rather than
    touching ``poll_once``/``apply_page`` themselves.
    """

    def _wrapped(task: Dict[str, Any]) -> Dict[str, Any]:
        result = base_apply_fn(task)
        if isinstance(result, dict) and "error" not in result:
            title = result.get("title") or (task or {}).get("title") or "(untitled)"
            _log(f"  applied {title!r} (global_id={result.get('global_id')!r})")
        return result

    return _wrapped


def _install_shutdown_handler(stop_event: "threading.Event") -> None:
    """Set ``stop_event`` on SIGINT or SIGTERM for a graceful loop exit.

    ``signal.signal`` only works on the main thread, but that is exactly where
    :func:`_run_loop` runs (a plain script/CLI process), matching
    ``outbox/drain.py run_forever``'s reliance on ``KeyboardInterrupt`` for the
    same purpose — SIGTERM additionally needs an explicit handler since it does
    not raise ``KeyboardInterrupt`` on its own.
    """

    def _handler(signum: int, frame: Any) -> None:
        _log(f"received signal {signum}; shutting down after the current poll")
        stop_event.set()

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def _run_loop(
    client: Any,
    repo_root: _PathLike,
    source_repository_id: str,
    interval: float,
    apply_fn: ApplyFn,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Poll every ``interval`` seconds until SIGINT/SIGTERM. Never raises: a
    transient network/HTTP/apply error (anything ``poll_once`` propagates) is
    logged and the loop continues — only a shutdown signal ends it, mirroring
    drain's "cursor held, keep retrying" resilience for the down-path.

    ``stop_event`` is injectable: when None (the CLI default) the loop owns the
    event and wires SIGINT/SIGTERM to it; when provided, the caller sets it to
    stop the loop — so interruptibility is testable cross-platform without OS
    signal delivery (``os.kill(pid, SIGINT)`` hard-kills the process on Windows).
    """
    if stop_event is None:
        stop_event = threading.Event()
        _install_shutdown_handler(stop_event)
    _log(
        f"starting down-sync loop (interval={interval}s, "
        f"source_repository_id={source_repository_id!r}, repo_root={repo_root})"
    )
    while not stop_event.is_set():
        try:
            applied = poll_once(client, repo_root, source_repository_id, apply_fn=apply_fn)
            _log(f"poll complete: {applied} task(s) applied")
        except Exception as exc:  # noqa: BLE001 - transient errors must not kill the loop
            _log(f"poll failed (will retry in {interval}s): {exc}")
        stop_event.wait(interval)
    _log("shutdown complete")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entrypoint: ``python mcp/task_inbox.py [--once]``.

    NOT ``python -m mcp.task_inbox`` — see the module docstring's "CLI" section
    for why that invocation cannot resolve. Flags/env mirror
    ``outbox/drain.py``'s ``main()`` (``--once``, ``--repo-root``, the
    ``AGENT_OS_API_URL``/``AGENT_OS_API_KEY`` env fallbacks) plus the
    down-path-specific ``--interval`` and ``--source-repository-id``.
    """
    parser = argparse.ArgumentParser(
        description="Pull + apply central task down-projections into the local "
        "card DB (the DOWN half of task sync; mirrors outbox.drain, the UP half)."
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Poll once, apply everything currently pending, then exit "
        "(for tests / manual runs).",
    )
    parser.add_argument(
        "--interval", type=float, default=_DEFAULT_INTERVAL,
        help=f"Seconds between polls in loop mode (default: {_DEFAULT_INTERVAL}).",
    )
    parser.add_argument(
        "--api-url", default=None,
        help=f"Agent OS API base URL (default: ${_URL_ENV} or {_DEFAULT_URL}).",
    )
    parser.add_argument(
        "--api-key", default=None,
        help=f"Agent OS API bearer key (default: ${_KEY_ENV}).",
    )
    parser.add_argument(
        "--source-repository-id", default=None,
        help="repository_id of the SOURCE repo whose tasks to pull "
        f"(default: ${_SOURCE_ENV}). Required (flag or env).",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=None,
        help="Local repository root to apply tasks into "
        "(default: walk up from cwd to .git).",
    )
    parser.add_argument(
        "--timeout", type=float, default=_DEFAULT_TIMEOUT,
        help=f"Per-request HTTP timeout in seconds (default: {_DEFAULT_TIMEOUT}).",
    )
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover - dotenv is a core dep
        pass

    api_url = args.api_url or os.environ.get(_URL_ENV) or _DEFAULT_URL
    api_key = args.api_key or os.environ.get(_KEY_ENV)
    source_repository_id = args.source_repository_id or os.environ.get(_SOURCE_ENV)
    if not source_repository_id:
        parser.error(f"--source-repository-id is required (or set {_SOURCE_ENV})")
    repo_root = Path(args.repo_root) if args.repo_root is not None else paths.find_repo_root()

    _bootstrap_local_card_db(repo_root)

    from services.m3_client.http import Client  # local import: needs repo root on sys.path

    client = Client(api_url, api_key, timeout=args.timeout)
    apply_fn = _logging_apply_fn(_resolve_apply_fn(None))

    if args.once:
        try:
            applied = poll_once(client, repo_root, source_repository_id, apply_fn=apply_fn)
        except Exception as exc:  # noqa: BLE001 - report, don't traceback, on --once
            _log(f"poll failed: {exc}")
            return 1
        _log(f"--once complete: {applied} task(s) applied")
        return 0

    _run_loop(client, repo_root, source_repository_id, args.interval, apply_fn)
    return 0


__all__ = [
    "INBOX_CURSOR_FILENAME",
    "read_inbox_cursors",
    "read_inbox_cursor",
    "write_inbox_cursor",
    "apply_page",
    "poll_once",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
