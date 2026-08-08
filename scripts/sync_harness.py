"""Cross-machine sync test harness (card f20398be).

``python scripts/sync_harness.py <new-card|up|down|status|roundtrip> [options]``

Collapses the per-command back-and-forth of testing cross-machine task sync
(create a card on one machine, drain it up, pull it down on another, eyeball
that it landed) into one committed, cross-platform CLI so any machine — human
or agent — runs one command and pastes the result. Existing sync logic is
NOT reimplemented here: this is a thin wrapper around

* :mod:`task_inbox` (``mcp/task_inbox.py``) — its ``_bootstrap_local_card_db``
  is reused verbatim for ``new-card``/``status`` (same DB-wiring the down-sync
  poller uses), and its own ``main()`` is reused verbatim for ``down``;
* :mod:`outbox.drain` — its own ``main()`` is reused verbatim for ``up``;
* :mod:`card_tools` — ``create_card`` / ``list_cards`` for ``new-card`` /
  ``status``;
* :class:`services.m3_client.http.Client` — the stdlib-only HTTP client, for
  ``status``'s central-tasks and registered-machines reads.

No poll/drain/apply logic lives in this module — only argument/env
resolution, local-DB bootstrap, and output rendering.

Direct-script invocation
-------------------------
Run as ``python scripts/sync_harness.py ...``, matching every other script in
this directory (``scripts/`` ships no ``__init__.py``, so ``-m
scripts.sync_harness`` does not resolve as a package). This module is OUTSIDE
``mcp/`` on purpose: it needs to ``import task_inbox`` / ``import card_tools``
as bare sibling modules (mirroring how the running MCP server imports them),
and the repo's ``mcp/`` directory shadows the pip-installed ``mcp`` SDK
package if a caller ever tried ``python -m mcp.task_inbox`` — see
``mcp/task_inbox.py``'s module docstring for the full "why". Living in
``scripts/`` sidesteps that collision entirely while still reaching ``mcp/``
via the explicit ``sys.path`` bootstrap below.

Config resolution
------------------
Same environment variable names/defaults as ``outbox/drain.py`` and
``mcp/task_inbox.py`` (loaded via ``python-dotenv`` first, like both of
those): ``AGENT_OS_API_URL`` (default ``http://127.0.0.1:8765``),
``AGENT_OS_API_KEY``, and ``AGENT_OS_SOURCE_REPOSITORY_ID`` (for ``down``).
A ``--repo-root``/``--api-url``/``--api-key`` flag always overrides its env
counterpart, matching ``task_inbox.py``'s own flag/env precedence.
"""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

# Bootstrap: put the repo root (for the top-level ``outbox``/``services``
# packages) AND ``mcp/`` (for the bare sibling imports ``card_tools`` /
# ``graph_io`` / ``task_inbox`` use internally) on sys.path. Mirrors
# ``mcp/tests/test_task_inbox_cli.py``'s bootstrap exactly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_MCP_DIR = _REPO_ROOT / "mcp"
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

from outbox import paths  # noqa: E402

_URL_ENV = "AGENT_OS_API_URL"
_KEY_ENV = "AGENT_OS_API_KEY"
_SOURCE_ENV = "AGENT_OS_SOURCE_REPOSITORY_ID"

_DEFAULT_URL = "http://127.0.0.1:8765"
_DEFAULT_TIMEOUT = 10.0
_DEFAULT_TITLE = "sync harness test card"
_DEFAULT_LOCAL_LIMIT = 5
_DEFAULT_CENTRAL_LIMIT = 20


def _load_dotenv() -> None:
    """Best-effort ``.env`` load — a standalone copy of the same try/except
    every other CLI entrypoint in this plugin (``outbox/drain.py``,
    ``mcp/task_inbox.py``) uses. Calling into ``drain.main()`` /
    ``task_inbox.main()`` below loads it again; that is idempotent and safe.
    """
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover - dotenv is a core dep
        pass


def _resolve_repo_root(raw: Optional[Path]) -> Path:
    return Path(raw) if raw is not None else paths.find_repo_root()


def _resolve_api_url(raw: Optional[str]) -> str:
    return raw or os.environ.get(_URL_ENV) or _DEFAULT_URL


def _resolve_api_key(raw: Optional[str]) -> Optional[str]:
    return raw or os.environ.get(_KEY_ENV)


def _bootstrap_card_db(repo_root: Path) -> None:
    """Wire ``card_tools``/``graph_io`` at this repo's local card DB, and
    ensure machine/repo sync identity exists.

    Delegates to ``task_inbox._bootstrap_local_card_db`` — the exact
    bootstrap the down-projection poller already uses (``set_db_path`` +
    ``init_db`` + ``graph_io.set_emission_repo_root``) — rather than
    reimplementing it (card f20398be: mirror, don't reinvent).

    Also bootstraps sync identity (see :func:`_bootstrap_sync_identity`):
    without it, ``card_tools.create_card``'s event emission is a silent
    no-op (``outbox.emit.emit_event`` treats "identity not bootstrapped" as
    expected and swallows it), so a card created through this harness would
    never actually queue for ``up`` to drain. A real MCP server session
    bootstraps identity at startup; this harness stands in for that.
    """
    # mcp/ is a sibling of scripts/, not an ancestor, so pyright's default
    # (config-less) namespace-package walk-up never sees it as a search root
    # even though the sys.path bootstrap above makes it resolve at runtime —
    # see the module docstring. Suppressed per import site rather than via a
    # repo-wide pyright config change (out of this card's scope).
    import task_inbox  # pyright: ignore[reportMissingImports]

    task_inbox._bootstrap_local_card_db(repo_root)
    _bootstrap_sync_identity(repo_root)


def _bootstrap_sync_identity(repo_root: Path) -> None:
    """Idempotently ensure machine/repo identity files exist (see
    ``outbox.identity.bootstrap_identity``), so an emitted event carries real
    ids instead of being silently dropped. Best-effort: a failure (e.g. the
    bundled ``contracts`` package unavailable) is reported to stderr but does
    not abort the caller — the card is still created locally, matching
    ``create_card``'s own degrade-gracefully-without-an-outbox behavior.
    """
    try:
        from outbox import identity as outbox_identity

        outbox_identity.bootstrap_identity(repo_root)
    except Exception as exc:  # pragma: no cover - defensive; see docstring
        print(
            f"warning: sync identity bootstrap failed ({exc}); "
            "the queued event may be a no-op",
            file=sys.stderr,
        )


@contextmanager
def _env_overrides(overrides: Dict[str, str]) -> Iterator[None]:
    """Temporarily set env vars, restoring the previous values on exit.

    Used only by ``up`` to let ``--api-url``/``--api-key`` flags reach
    ``outbox.drain.main()``, whose own argparse (deliberately, upstream)
    exposes no such flags and reads only the environment. Scoped to a single
    synchronous call in a one-shot CLI process, so there is no concurrent
    access to race against.
    """
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        os.environ.update(overrides)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# --------------------------------------------------------------------------- #
# new-card
# --------------------------------------------------------------------------- #
def cmd_new_card(args: argparse.Namespace) -> int:
    """Bootstrap the local card DB and create one card, printing its ids.

    Replaces the ad-hoc hand-written ``m2_card.py`` invocations: mints a
    ``global_id`` and queues the ``task.created`` outbox event exactly like
    the ``create_card`` MCP tool does (same function, same side effects).
    """
    repo_root = _resolve_repo_root(args.repo_root)
    _bootstrap_card_db(repo_root)
    import card_tools  # pyright: ignore[reportMissingImports] -- see _bootstrap_card_db

    result = card_tools.create_card(title=args.title, priority="medium")
    if "error" in result:
        print(f"new-card failed: {result['error']}", file=sys.stderr)
        return 1
    print(
        f"new-card: card_id={result['card_id']} "
        f"global_id={result.get('global_id')} title={result['title']!r}"
    )
    return 0


# --------------------------------------------------------------------------- #
# up
# --------------------------------------------------------------------------- #
def cmd_up(args: argparse.Namespace) -> int:
    """Drain the local outbox to the central API — a thin wrap of
    ``outbox.drain --once``; poll/drain logic is untouched."""
    from outbox import drain

    repo_root = _resolve_repo_root(args.repo_root)
    argv = ["--once", "--repo-root", str(repo_root)]
    overrides: Dict[str, str] = {}
    if args.api_url:
        overrides[_URL_ENV] = args.api_url
    if args.api_key:
        overrides[_KEY_ENV] = args.api_key
    with _env_overrides(overrides):
        rc = drain.main(argv)
    print(f"up: drain {'succeeded' if rc == 0 else f'failed (exit {rc})'}")
    return rc


# --------------------------------------------------------------------------- #
# down
# --------------------------------------------------------------------------- #
def cmd_down(args: argparse.Namespace) -> int:
    """Pull + apply central tasks into the local card DB — a thin wrap of
    ``mcp/task_inbox.py --once``; poll/apply logic is untouched."""
    import task_inbox  # pyright: ignore[reportMissingImports] -- see _bootstrap_card_db

    repo_root = _resolve_repo_root(args.repo_root)
    source_repository_id = args.source_repository_id or os.environ.get(_SOURCE_ENV)
    if not source_repository_id:
        print(
            f"down failed: --source-repository-id is required (or set {_SOURCE_ENV})",
            file=sys.stderr,
        )
        return 1

    argv = [
        "--once", "--repo-root", str(repo_root),
        "--source-repository-id", source_repository_id,
    ]
    if args.api_url:
        argv += ["--api-url", args.api_url]
    if args.api_key:
        argv += ["--api-key", args.api_key]
    rc = task_inbox.main(argv)
    print(f"down: pull+apply {'succeeded' if rc == 0 else f'failed (exit {rc})'}")
    return rc


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def _gather_status(
    repo_root: Path, api_url: str, api_key: Optional[str], timeout: float,
    *, local_limit: int, central_limit: int,
) -> Dict[str, Any]:
    """One combined read: local cards (via ``card_tools``) + central tasks +
    registered machines (via the stdlib-only ``services.m3_client.http.Client``,
    the same client ``mcp/task_inbox.py`` uses for the down-poll)."""
    _bootstrap_card_db(repo_root)
    import card_tools  # pyright: ignore[reportMissingImports] -- see _bootstrap_card_db
    from services.m3_client.http import ApiError, Client

    local = card_tools.list_cards()
    local_cards = local.get("cards", [])
    data: Dict[str, Any] = {
        "repo_root": str(repo_root),
        "api_url": api_url,
        "local": {
            "total": local.get("total", len(local_cards)),
            "recent": local_cards[:local_limit],
        },
        "central": {"total": None, "recent": [], "error": None},
        "machines": {"total": None, "items": [], "error": None},
    }

    client = Client(api_url, api_key, timeout=timeout)
    try:
        tasks_body = client.get_tasks(limit=central_limit)
        tasks = tasks_body.get("tasks") or []
        data["central"]["total"] = len(tasks)
        data["central"]["recent"] = tasks[:central_limit]
    except ApiError as exc:
        data["central"]["error"] = str(exc)

    try:
        machines = client.get_machines()
        data["machines"]["total"] = len(machines)
        data["machines"]["items"] = machines
    except ApiError as exc:
        data["machines"]["error"] = str(exc)

    return data


def render_status(data: Dict[str, Any]) -> str:
    """Plain-text local-vs-central glance from a :func:`_gather_status` dict."""
    # Sanitize terminal-injectable fields at the render boundary (machine name/os
    # are self-reported by OTHER machines) — matches services/m3_client/render.py.
    from services.m3_client.render import _sanitize  # pyright: ignore[reportMissingImports]

    lines: List[str] = [
        f"Agent OS sync harness -- status  (repo_root={data['repo_root']})",
        f"API: {data['api_url']}",
        "",
    ]

    local = data["local"]
    lines.append(f"LOCAL CARDS ({local['total']})")
    if not local["recent"]:
        lines.append("  (none)")
    for card in local["recent"]:
        lines.append(
            f"  {card.get('card_id', '')}  global_id={card.get('global_id') or '-'}  "
            f"status={card.get('status', '')}  {card.get('title', '')!r}"
        )
    lines.append("")

    central = data["central"]
    if central["error"]:
        lines.append(f"CENTRAL TASKS: unavailable ({central['error']})")
    else:
        lines.append(f"CENTRAL TASKS ({central['total']})")
        if not central["recent"]:
            lines.append("  (none)")
        for task in central["recent"]:
            lines.append(
                f"  global_id={task.get('global_id', '')}  status={task.get('status', '')}  "
                f"{task.get('title', '')!r}"
            )
    lines.append("")

    machines = data["machines"]
    if machines["error"]:
        lines.append(f"MACHINES: unavailable ({machines['error']})")
    else:
        lines.append(f"MACHINES ({machines['total']})")
        if not machines["items"]:
            lines.append("  (none)")
        for machine in machines["items"]:
            lines.append(
                f"  {_sanitize(machine.get('machine_id', ''))}  "
                f"{_sanitize(machine.get('name', ''))}  {_sanitize(machine.get('os', ''))}"
            )

    if not central["error"]:
        central_ids = {task.get("global_id") for task in central["recent"]}
        matched = sum(
            1 for card in local["recent"]
            if card.get("global_id") and card.get("global_id") in central_ids
        )
        lines.append("")
        lines.append(
            f"glance: {matched}/{len(local['recent'])} of the recent local cards shown "
            "above also appear in the central tasks shown above"
        )

    return "\n".join(lines)


def cmd_status(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args.repo_root)
    api_url = _resolve_api_url(args.api_url)
    api_key = _resolve_api_key(args.api_key)
    data = _gather_status(
        repo_root, api_url, api_key, args.timeout,
        local_limit=args.local_limit, central_limit=args.central_limit,
    )
    print(render_status(data))
    return 0


# --------------------------------------------------------------------------- #
# roundtrip
# --------------------------------------------------------------------------- #
def cmd_roundtrip(args: argparse.Namespace) -> int:
    """``new-card`` -> ``up`` -> ``status``, stopping at the first failure —
    confirms a freshly created card actually materializes centrally."""
    print("== new-card ==")
    rc = cmd_new_card(args)
    if rc != 0:
        return rc
    print()
    print("== up ==")
    rc = cmd_up(args)
    if rc != 0:
        return rc
    print()
    print("== status ==")
    return cmd_status(args)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    repo_parent = argparse.ArgumentParser(add_help=False)
    repo_parent.add_argument(
        "--repo-root", type=Path, default=None,
        help="Repository root (default: walk up from cwd to .git).",
    )

    api_parent = argparse.ArgumentParser(add_help=False)
    api_parent.add_argument(
        "--api-url", default=None,
        help=f"Agent OS API base URL (default: ${_URL_ENV} or {_DEFAULT_URL}).",
    )
    api_parent.add_argument(
        "--api-key", default=None,
        help=f"Agent OS API bearer key (default: ${_KEY_ENV}).",
    )
    api_parent.add_argument(
        "--timeout", type=float, default=_DEFAULT_TIMEOUT,
        help=f"Per-request HTTP timeout in seconds (default: {_DEFAULT_TIMEOUT}).",
    )

    new_card_parent = argparse.ArgumentParser(add_help=False)
    new_card_parent.add_argument(
        "--title", default=_DEFAULT_TITLE,
        help=f"Card title (default: {_DEFAULT_TITLE!r}).",
    )

    status_parent = argparse.ArgumentParser(add_help=False)
    status_parent.add_argument(
        "--local-limit", type=int, default=_DEFAULT_LOCAL_LIMIT,
        help=f"Recent local cards to show (default: {_DEFAULT_LOCAL_LIMIT}).",
    )
    status_parent.add_argument(
        "--central-limit", type=int, default=_DEFAULT_CENTRAL_LIMIT,
        help=f"Recent central tasks to fetch/show (default: {_DEFAULT_CENTRAL_LIMIT}).",
    )

    down_parent = argparse.ArgumentParser(add_help=False)
    down_parent.add_argument(
        "--source-repository-id", default=None,
        help="repository_id of the SOURCE repo whose tasks to pull "
        f"(default: ${_SOURCE_ENV}). Required (flag or env).",
    )

    parser = argparse.ArgumentParser(
        prog="python scripts/sync_harness.py",
        description="Cross-machine sync test harness: one-command "
        "new-card / up / down / status / roundtrip (card f20398be).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_new_card = subparsers.add_parser(
        "new-card", parents=[repo_parent, new_card_parent],
        help="Create a local card; mints a global_id and queues its sync event.",
    )
    p_new_card.set_defaults(func=cmd_new_card)

    p_up = subparsers.add_parser(
        "up", parents=[repo_parent, api_parent],
        help="Drain the local outbox to the central API.",
    )
    p_up.set_defaults(func=cmd_up)

    p_down = subparsers.add_parser(
        "down", parents=[repo_parent, api_parent, down_parent],
        help="Pull + apply central tasks into the local card DB.",
    )
    p_down.set_defaults(func=cmd_down)

    p_status = subparsers.add_parser(
        "status", parents=[repo_parent, api_parent, status_parent],
        help="Local-vs-central glance: local cards, central tasks, registered machines.",
    )
    p_status.set_defaults(func=cmd_status)

    p_roundtrip = subparsers.add_parser(
        "roundtrip", parents=[repo_parent, api_parent, new_card_parent, status_parent],
        help="new-card -> up -> status in one shot.",
    )
    p_roundtrip.set_defaults(func=cmd_roundtrip)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _load_dotenv()
    return args.func(args)


__all__ = [
    "build_parser",
    "cmd_new_card",
    "cmd_up",
    "cmd_down",
    "cmd_status",
    "cmd_roundtrip",
    "render_status",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
