"""Agent OS versioned HTTP API (``/v1``) in front of the central store.

The §17 first slice's *control-plane read/write surface*: machine / repository /
session registration, session heartbeats, and idempotent event ingestion, plus
the read endpoints a stand-in M3 client (card 1d) consumes. Runs on Machine 1
this phase (``python -m services.api``); a LAN bind for M2/M3 is a later-phase
concern (documented in ``services/README.md``).

Design posture (held to the epic's governing decisions):

* **Local-first / never raw tables.** External clients reach the central DB only
  through this versioned API, never raw SQL (§18.1, §13).
* **Tolerant inputs, strict outputs.** Request bodies are validated with the
  ``contracts`` models (``extra="ignore"`` — a newer client's extra fields never
  break an older server); responses are built explicitly, never echoing arbitrary
  input.
* **Auth on every ``/v1`` route.** A static bearer key (``AGENT_OS_API_KEY``)
  compared in constant time; no credentials in code.
* **Per-request DB connection** via :func:`database.migrate.connect` (which carries
  the DATETIMEOFFSET output converter); all writes parameterized; errors sanitized
  (no internals in responses).

The Flask app is built by :func:`services.api.app.create_app`, which takes an
injectable :class:`~services.api.store.Store` so the endpoint tests run against a
pure in-memory fake with no database.
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
