"""Run the Agent OS ``/v1`` API: ``python -m services.api``.

Binds ``AGENT_OS_API_HOST``:``AGENT_OS_API_PORT`` (default ``127.0.0.1:8765`` —
loopback only this phase; a LAN bind for M2/M3 is a later-phase concern, see
``services/README.md``). Reads the bearer key from ``AGENT_OS_API_KEY`` and the
central-DB connection string from ``AGENT_OS_CENTRAL_DB_CONNECTION_STRING`` (both
via ``.env`` / the process environment — never hardcoded).

``threaded=True`` is safe here: the service holds no shared mutable state and each
request opens its own DB connection (see :mod:`services.api.app`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure the repo root is importable (contracts / database / services) regardless
# of the caller's cwd — same convention as the rest of the repo.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is a core dep, but stay importable
    pass

from services.api.app import create_app  # noqa: E402

_HOST_ENV = "AGENT_OS_API_HOST"
_PORT_ENV = "AGENT_OS_API_PORT"
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8765


def _host() -> str:
    return os.environ.get(_HOST_ENV) or _DEFAULT_HOST


def _port() -> int:
    raw = os.environ.get(_PORT_ENV)
    if not raw:
        return _DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        return _DEFAULT_PORT


def main() -> int:
    app = create_app()
    if not app.config.get("AGENT_OS_API_KEY") and not os.environ.get("AGENT_OS_API_KEY"):
        # Not fatal (the app fails every request closed with 401), but warn loudly
        # so a misconfigured deploy is obvious rather than silently rejecting all.
        print(
            "WARNING: AGENT_OS_API_KEY is not set — every /v1 request will 401.",
            file=sys.stderr,
        )
    host, port = _host(), _port()
    print(f"Agent OS API listening on http://{host}:{port} (/v1)", file=sys.stderr)
    app.run(host=host, port=port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
