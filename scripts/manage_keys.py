"""Admin CLI to issue / revoke / list per-machine API keys (card 8737706e).

The operator-facing companion to the per-machine credential endpoints on the
central ``/v1`` API. Run it with the SHARED admin key (``AGENT_OS_API_KEY``) —
the same bootstrap key used to register machines — since credential management is
admin-only::

    python scripts/manage_keys.py issue  --machine-id mach_01J...  --label m3-drain
    python scripts/manage_keys.py list    --machine-id mach_01J...
    python scripts/manage_keys.py revoke  --machine-id mach_01J...  --key-id key_01J...

Config resolution mirrors the other clients (``outbox/drain.py`` /
``mcp/task_inbox.py``): ``--api-url`` / ``--api-key`` flags fall back to the
``AGENT_OS_API_URL`` / ``AGENT_OS_API_KEY`` environment (``.env`` is loaded if
``python-dotenv`` is installed). Transport is the stdlib-only
:class:`services.m3_client.http.Client`.

Direct-script invocation (``python scripts/manage_keys.py``), NOT ``python -m`` —
the same convention as ``mcp/task_inbox.py``: this file bootstraps the repo root
onto ``sys.path`` so the top-level ``services`` package resolves regardless of the
caller's working directory.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

# Put the repo root on sys.path so ``services`` resolves when run as a direct
# script from any working directory (mirrors mcp/task_inbox.py's bootstrap).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_URL_ENV = "AGENT_OS_API_URL"
_KEY_ENV = "AGENT_OS_API_KEY"
_DEFAULT_URL = "http://127.0.0.1:8765"
_DEFAULT_TIMEOUT = 10.0


def _print_issued_key(machine_id: str, result: dict) -> None:
    """Print a freshly-issued key prominently with the one-time-visibility warning."""
    key = result.get("key") or f"{result.get('key_id')}.{result.get('secret')}"
    banner = "=" * 68
    print(banner)
    print("  Per-machine API key issued")
    print(f"  machine_id : {machine_id}")
    print(f"  key_id     : {result.get('key_id')}")
    print()
    print("  FULL KEY (set as AGENT_OS_API_KEY on that machine):")
    print(f"    {key}")
    print()
    print("  STORE THIS NOW. The secret is shown ONCE and cannot be retrieved")
    print("  again — only its hash is stored server-side. If lost, revoke this")
    print("  key_id and issue a new one.")
    print(banner)


def _print_credentials(machine_id: str, creds: List[dict]) -> None:
    if not creds:
        print(f"No credentials for machine {machine_id}.")
        return
    print(f"Credentials for machine {machine_id}:")
    for c in creds:
        state = "REVOKED" if c.get("revoked_at") else "active"
        label = c.get("label") or "-"
        last_used = c.get("last_used_at") or "never"
        print(
            f"  {c.get('key_id')}  [{state}]  label={label}  "
            f"created={c.get('created_at')}  last_used={last_used}"
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue / revoke / list per-machine API keys on the central "
        "/v1 API (run with the shared admin AGENT_OS_API_KEY)."
    )
    parser.add_argument(
        "--api-url", default=None,
        help=f"Agent OS API base URL (default: ${_URL_ENV} or {_DEFAULT_URL}).",
    )
    parser.add_argument(
        "--api-key", default=None,
        help=f"Admin bearer key (default: ${_KEY_ENV}).",
    )
    parser.add_argument(
        "--timeout", type=float, default=_DEFAULT_TIMEOUT,
        help=f"Per-request HTTP timeout in seconds (default: {_DEFAULT_TIMEOUT}).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_issue = sub.add_parser("issue", help="Issue a new per-machine key.")
    p_issue.add_argument("--machine-id", required=True, help="Target machine id.")
    p_issue.add_argument("--label", default=None, help="Optional human label.")

    p_revoke = sub.add_parser("revoke", help="Revoke a per-machine key.")
    p_revoke.add_argument("--machine-id", required=True, help="Owning machine id.")
    p_revoke.add_argument("--key-id", required=True, help="key_id to revoke.")

    p_list = sub.add_parser("list", help="List a machine's keys (never secrets).")
    p_list.add_argument("--machine-id", required=True, help="Machine id to list.")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover - dotenv is a core dep
        pass

    api_url = args.api_url or os.environ.get(_URL_ENV) or _DEFAULT_URL
    api_key = args.api_key or os.environ.get(_KEY_ENV)
    if not api_key:
        parser.error(f"an admin API key is required (--api-key or ${_KEY_ENV})")

    # Local import: needs the repo root on sys.path (bootstrapped above).
    from services.m3_client.http import ApiError, Client

    client = Client(api_url, api_key, timeout=args.timeout)

    try:
        if args.command == "issue":
            result = client.issue_credential(args.machine_id, label=args.label)
            _print_issued_key(args.machine_id, result)
        elif args.command == "revoke":
            client.revoke_credential(args.machine_id, args.key_id)
            print(f"Revoked {args.key_id} on machine {args.machine_id}.")
        elif args.command == "list":
            creds = client.list_credentials(args.machine_id)
            _print_credentials(args.machine_id, creds)
        else:  # pragma: no cover - argparse enforces a known subcommand
            parser.error(f"unknown command: {args.command}")
    except ApiError as exc:
        detail = f" (code={exc.code})" if exc.code else ""
        print(f"error: {exc}{detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
