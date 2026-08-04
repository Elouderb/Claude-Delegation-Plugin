"""Stand-in M3 read-only status client — ``python -m services.m3_client status``.

The §17 slice's final piece (card 93baf05b, epic 78a6ac11): a small terminal
client playing Machine 3's role until the real M3 VM and communication agent
exist (Phase 6). Reads machines, repositories, active agent sessions, and
recent events from the Agent OS ``/v1`` API and renders a compact plain-text
board — proving the control-plane READ path end-to-end.

Unlike ``services/api`` and ``outbox/drain`` (which run on M1 and load ``.env``
for developer convenience), this package deliberately never imports
``python-dotenv`` — or anything else outside the standard library — because it
must eventually run standalone on a bare-Python M3 VM with none of this
repo's other dependencies installed. Configure it via the process environment
(``AGENT_OS_API_URL`` / ``AGENT_OS_API_KEY``, e.g. exported in the shell or a
systemd unit) or the ``--api-url`` / ``--api-key`` flags.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .follow import walk_new_events
from .http import ApiError, Client
from .render import render_board, render_event_line

_URL_ENV = "AGENT_OS_API_URL"
_KEY_ENV = "AGENT_OS_API_KEY"
_DEFAULT_URL = "http://127.0.0.1:8765"
_DEFAULT_CLIENT_ID = "m3-standin"
_DEFAULT_LIMIT = 20
_DEFAULT_INTERVAL = 5.0


def _log(message: str) -> None:
    """Timestamped stderr line — stdout is reserved for board/event output so
    ``--json`` piping to another tool never sees log noise mixed in."""
    try:
        stamp = datetime.now(timezone.utc).isoformat()
        print(f"{stamp} [m3-client] {message}", file=sys.stderr, flush=True)
    except Exception:  # pragma: no cover - stderr closed / encoding edge
        pass


def _snapshot(client: Client, *, limit: int, repository_id: Optional[str]) -> Dict[str, Any]:
    """One combined read of the whole board's data (four /v1 GETs)."""
    return {
        "machines": client.get_machines(),
        "repositories": client.get_repositories(),
        "sessions": client.get_sessions(active=True),
        "events": client.get_recent_events(limit=limit, repository_id=repository_id)["events"],
        "api_url": client.base_url,
    }


def _print_snapshot(data: Dict[str, Any], *, as_json: bool, now: Optional[datetime]) -> None:
    # flush=True: stdout is fully buffered (not line-buffered) once redirected
    # to a file/pipe (e.g. nohup, `| tee`) rather than a terminal, so without an
    # explicit flush a human tailing --follow's output would see nothing until
    # the OS buffer filled or the process exited - defeating the "watch it live" point.
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True), flush=True)
    else:
        print(render_board(data, now=now), flush=True)


def _run_status(args: argparse.Namespace) -> int:
    client = Client(args.api_url, args.api_key, timeout=args.timeout)
    try:
        data = _snapshot(client, limit=args.limit, repository_id=args.repository_id)
    except ApiError as exc:
        _log(f"request failed: {exc}")
        return 1
    _print_snapshot(data, as_json=args.json, now=None)

    if not args.follow:
        return 0
    return _run_follow(client, args, initial_events=data["events"])


def _run_follow(client: Client, args: argparse.Namespace, *, initial_events: List[Dict[str, Any]]) -> int:
    """Poll for new events after the initial board render, using the after_seq
    keyset walk (:mod:`services.m3_client.follow`) so a burst larger than one
    page is never dropped, and persisting position via ``--client-id`` so a
    restart resumes instead of replaying everything already shown."""
    # events are newest-first (ingest_seq DESC); [0] is the highest seq seen.
    cursor: Optional[int] = initial_events[0].get("ingest_seq") if initial_events else None
    try:
        remote = client.get_sync_cursor(args.client_id)
        if remote.get("last_seq") is not None:
            cursor = remote["last_seq"]
    except ApiError as exc:
        _log(f"could not load sync cursor {args.client_id!r} (starting from this snapshot): {exc}")

    def fetch_page(after_seq: Optional[int]) -> Dict[str, Any]:
        return client.get_recent_events(
            limit=args.limit, repository_id=args.repository_id, after_seq=after_seq,
        )

    _log(f"following (client_id={args.client_id!r}, interval={args.interval}s); Ctrl-C to stop")
    try:
        while True:
            time.sleep(args.interval)
            try:
                new_events, cursor = walk_new_events(fetch_page, cursor)
            except ApiError as exc:
                _log(f"poll failed (will retry): {exc}")
                continue

            for event in new_events:
                line = json.dumps(event, sort_keys=True) if args.json else render_event_line(event)
                print(line, flush=True)

            if new_events:
                last_event_id = new_events[-1].get("event_id")
                try:
                    client.put_sync_cursor(
                        args.client_id, last_seq=cursor, last_event_id=last_event_id,
                    )
                except ApiError as exc:
                    _log(f"could not persist sync cursor (ignored): {exc}")
    except KeyboardInterrupt:
        _log("interrupted; exiting")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.m3_client",
        description="Stand-in M3 read-only status client for the Agent OS /v1 API (card 93baf05b).",
    )
    parser.add_argument(
        "--api-url", default=os.environ.get(_URL_ENV) or _DEFAULT_URL,
        help=f"Agent OS API base URL (default: ${_URL_ENV} or {_DEFAULT_URL}).",
    )
    parser.add_argument(
        "--api-key", default=os.environ.get(_KEY_ENV),
        help=f"Bearer key (default: ${_KEY_ENV}).",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="Per-request timeout, seconds.")

    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status", help="Render the machines/repos/sessions/events board.")
    status.add_argument(
        "--json", action="store_true",
        help="Emit raw JSON instead of the plain-text board (the Telegram/M3-agent integration seam).",
    )
    status.add_argument(
        "--follow", action="store_true",
        help="After the initial board, keep polling and print new events as they arrive.",
    )
    status.add_argument(
        "--interval", type=float, default=_DEFAULT_INTERVAL, help="--follow poll interval, seconds.",
    )
    status.add_argument("--limit", type=int, default=_DEFAULT_LIMIT, help="Recent-events page size.")
    status.add_argument("--repository-id", default=None, help="Filter recent events to one repository.")
    status.add_argument(
        "--client-id", default=_DEFAULT_CLIENT_ID,
        help="Sync-cursor client id for --follow persistence (default: %(default)s).",
    )
    status.set_defaults(func=_run_status)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
