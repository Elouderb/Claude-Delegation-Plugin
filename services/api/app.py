"""Flask app factory for the Agent OS ``/v1`` API (Phase 1c, epic 78a6ac11).

:func:`create_app` builds the app with an injectable :class:`~services.api.store.Store`
and API key, so the endpoint tests run against a pure in-memory fake. All routes
under ``/v1`` require a static bearer key (constant-time compare); a single
unauthenticated ``GET /health`` liveness probe (which exposes no data and touches
no database) is the one deliberate exception, mirroring the graph server's
``/health``.

Threading: this service is safe to run with Flask's ``threaded=True`` because it
holds no shared mutable state and every request opens its own DB connection inside
its one store method (unlike the graph UI, which must stay single-threaded). See
:mod:`services.api.__main__`.

Input posture is tolerant-reader: request bodies are validated with the
``contracts`` models (``extra="ignore"``), so an older server still parses a newer
client's extra fields. Outputs are built explicitly and errors are sanitized —
responses never carry stack traces, SQL, or connection details.
"""

from __future__ import annotations

import hmac
import json
import os
from typing import Any, Optional, Tuple

from flask import Flask, jsonify, request
from pydantic import ValidationError

from contracts.events import EventEnvelope
from contracts.identity import MachineIdentity, RepositoryIdentity
from contracts.registry import AgentRegistration

from .store import ALLOWED_STATUSES, PreparedEvent

_API_KEY_ENV = "AGENT_OS_API_KEY"
_BEARER_PREFIX = "Bearer "
_MAX_RAW_BYTES = 8192  # cap on the ingest-error raw snapshot we keep per event
_RECENT_LIMIT_DEFAULT = 50
_RECENT_LIMIT_MAX = 500

# Batch/body caps (finding: unbounded /v1/events/batch). ``_MAX_BATCH_EVENTS``
# matches the drain's own default AGENT_OS_SYNC_BATCH; ``_MAX_CONTENT_LENGTH`` is
# the Flask body ceiling that comfortably holds a max batch of max-size events
# (500 × 64 KiB outbox-line cap) — Werkzeug rejects a larger body with 413.
_MAX_BATCH_EVENTS = 500
_MAX_CONTENT_LENGTH = 40 * 1024 * 1024  # 40 MiB

# ops.sync_cursors.client_id is NVARCHAR(64) (the repo's uniform id-column
# width, see database/README.md); reject an oversized id at the app layer with
# a clean 400 instead of letting a driver-level truncation error surface as 500.
_MAX_CLIENT_ID_CHARS = 64

# ops.sync_cursors.last_seq is a SQL BIGINT (see database/migrations/005). Reject
# an out-of-range value (negative, or beyond BIGINT max) at the app layer with a
# clean 400: a negative value would otherwise be accepted and later make a
# --follow keyset walk treat it as a valid-but-ancient cursor, replaying the
# entire event history up to its max_pages bound (code review, card 93baf05b);
# an overflowing value would instead reach the SQL Server driver and surface as
# a generic sanitized 500 rather than a client-correctable 400.
_MIN_LAST_SEQ = 0
_MAX_LAST_SEQ = 9223372036854775807  # SQL Server BIGINT max


def create_app(*, api_key: Optional[str] = None, store: Optional[Any] = None) -> Flask:
    """Build the Agent OS API Flask app.

    ``api_key`` defaults to the ``AGENT_OS_API_KEY`` environment variable. It may
    be a single key OR a comma-separated LIST of keys — every listed key is
    accepted, so a key can be rotated with an overlap window (add the new key,
    redeploy clients, then drop the old). If it resolves to empty, every ``/v1``
    request fails closed with 401 (a server with no key configured can
    authenticate nobody). ``store`` defaults to a
    :class:`~services.api.store.SqlStore` over :func:`database.migrate.connect`;
    tests inject a :class:`~services.api.store.FakeStore`.
    """
    app = Flask(__name__)
    resolved_key = api_key if api_key is not None else os.environ.get(_API_KEY_ENV)
    app.config["AGENT_OS_API_KEY"] = resolved_key
    app.config["MAX_CONTENT_LENGTH"] = _MAX_CONTENT_LENGTH
    accepted_keys = _parse_keys(resolved_key)

    if store is None:
        from database import migrate

        from .store import SqlStore

        store = SqlStore(migrate.connect)

    # ----------------------------------------------------------------- auth #
    @app.before_request
    def _require_auth():
        if request.path == "/health":
            return None
        header = request.headers.get("Authorization", "")
        if not _authorized(header, accepted_keys):
            return _error(401, "unauthorized", "unauthorized")
        return None

    # ----------------------------------------------------------- liveness #
    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "service": "agent-os-api"}), 200

    # ------------------------------------------------------- registration #
    @app.post("/v1/machines/register")
    def register_machine():
        data, err = _json_object()
        if err is not None:
            return err
        try:
            ident = MachineIdentity.model_validate(data)
        except ValidationError as exc:
            return _error(400, "invalid_payload", f"invalid machine payload: {_validation_detail(exc)}")
        result = store.upsert_machine(
            machine_id=ident.machine_id, name=ident.name, os=ident.os,
            created_at=ident.created_at,
        )
        return jsonify({
            "machine_id": result["machine_id"],
            "status": "created" if result["created"] else "updated",
        }), 200

    @app.post("/v1/repositories/register")
    def register_repository():
        data, err = _json_object()
        if err is not None:
            return err
        try:
            ident = RepositoryIdentity.model_validate(data)
        except ValidationError as exc:
            return _error(400, "invalid_payload", f"invalid repository payload: {_validation_detail(exc)}")
        repo_root = data.get("repo_root")
        if not isinstance(repo_root, str) or not repo_root.strip():
            return _error(400, "invalid_payload", "repo_root is required")
        if not ident.machine_id:
            return _error(400, "invalid_payload", "machine_id is required")
        result = store.upsert_repository(
            repository_id=ident.repository_id, machine_id=ident.machine_id,
            repo_root=repo_root, canonical_remote=ident.canonical_remote,
            project_id=ident.project_id, created_at=ident.created_at,
        )
        return jsonify({
            "repository_id": result["repository_id"],
            "status": "created" if result["created"] else "updated",
            "remapped": result["remapped"],
        }), 200

    @app.post("/v1/sessions/register")
    def register_session():
        data, err = _json_object()
        if err is not None:
            return err
        try:
            reg = AgentRegistration.model_validate(data)
        except ValidationError as exc:
            return _error(400, "invalid_payload", f"invalid session payload: {_validation_detail(exc)}")
        result = store.upsert_session(
            session_id=reg.session_id, agent_id=reg.agent_id, machine_id=reg.machine_id,
            repository_id=reg.repository_id, agent_type=reg.agent_type,
            parent_agent_id=reg.parent_agent_id, capabilities=list(reg.capabilities),
            current_task_id=reg.current_task_id, status=reg.status.value,
            model_runtime=reg.runtime, started_at=reg.started_at,
            heartbeat_at=reg.heartbeat_at, expires_at=reg.expected_expiration_at,
        )
        return jsonify({
            "session_id": result["session_id"],
            "status": "created" if result["created"] else "updated",
        }), 200

    @app.post("/v1/sessions/<session_id>/heartbeat")
    def heartbeat_session(session_id: str):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            data = {}
        status = data.get("status")
        if status is not None and status not in ALLOWED_STATUSES:
            return _error(400, "invalid_status", "invalid status")
        current_task_id = data.get("current_task_id")
        result = store.heartbeat_session(
            session_id=session_id, status=status, current_task_id=current_task_id
        )
        if result is None:
            return _error(404, "not_found", "session not found")
        # ``result`` (not ``status``) is the transport ack — the resource's own
        # lifecycle status lives under its own key, so the two never collide.
        return jsonify({
            "session_id": session_id, "result": "ok",
            "heartbeat_at": result["heartbeat_at"],
        }), 200

    # -------------------------------------------------------- event ingest #
    @app.post("/v1/events/batch")
    def ingest_batch():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error(400, "invalid_payload", "expected a JSON object with an 'events' list")
        events = data.get("events")
        if not isinstance(events, list):
            return _error(400, "invalid_payload", "'events' must be a list")
        if len(events) > _MAX_BATCH_EVENTS:
            return _error(
                413, "batch_too_large",
                f"batch has {len(events)} events; the maximum is {_MAX_BATCH_EVENTS}",
            )
        prepared = [_prepare_event(item) for item in events]
        try:
            outcomes = store.ingest_events(prepared)
        except Exception:
            # A transport / DB failure — return 5xx WITHOUT a per-event verdict so
            # the drain does not advance its cursor. Log server-side; never leak the
            # cause to the client.
            app.logger.exception("event ingest failed")
            return _error(500, "internal_error", "event ingestion failed")
        return jsonify({"results": outcomes}), 200

    # ------------------------------------------------------------- reads #
    @app.get("/v1/machines")
    def list_machines():
        return jsonify({"machines": store.list_machines()}), 200

    @app.get("/v1/repositories")
    def list_repositories():
        machine_id = request.args.get("machine_id") or None
        return jsonify({"repositories": store.list_repositories(machine_id=machine_id)}), 200

    @app.get("/v1/sessions")
    def list_sessions():
        active = _truthy(request.args.get("active"))
        return jsonify({"sessions": store.list_sessions(active=active)}), 200

    @app.get("/v1/events/recent")
    def recent_events():
        limit = _parse_limit(request.args.get("limit"))
        repository_id = request.args.get("repository_id") or None
        event_type = request.args.get("event_type") or None
        after_seq = _parse_after_seq(request.args.get("after_seq"))
        events = store.recent_events(
            limit=limit, repository_id=repository_id, event_type=event_type,
            after_seq=after_seq,
        )
        # Keyset continuation: the smallest ingest_seq in this page. A poller passes
        # it back as ?after_seq= to fetch strictly-older events with no overlap and
        # no dropped burst. ``null`` when the page is empty (caller is caught up).
        next_after = events[-1].get("ingest_seq") if events else None
        return jsonify({"events": events, "next_after": next_after}), 200

    # -------------------------------------------------------- sync cursors #
    @app.get("/v1/sync/<client_id>")
    def get_sync_cursor(client_id: str):
        if len(client_id) > _MAX_CLIENT_ID_CHARS:
            return _error(400, "invalid_payload", "client_id is too long")
        result = store.get_sync_cursor(client_id=client_id)
        if result is None:
            # No recorded position yet - not an error; a fresh consumer starts
            # from "now" rather than being forced to special-case a 404.
            result = {
                "client_id": client_id, "last_seq": None, "last_event_id": None,
                "updated_at": None,
            }
        return jsonify(result), 200

    @app.put("/v1/sync/<client_id>")
    def put_sync_cursor(client_id: str):
        if len(client_id) > _MAX_CLIENT_ID_CHARS:
            return _error(400, "invalid_payload", "client_id is too long")
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error(400, "invalid_payload", "expected a JSON object")
        last_seq = data.get("last_seq")
        if last_seq is not None:
            try:
                last_seq = int(last_seq)
            except (TypeError, ValueError):
                return _error(400, "invalid_payload", "last_seq must be an integer")
            if not (_MIN_LAST_SEQ <= last_seq <= _MAX_LAST_SEQ):
                return _error(
                    400, "invalid_payload",
                    f"last_seq must be between {_MIN_LAST_SEQ} and {_MAX_LAST_SEQ}",
                )
        last_event_id = data.get("last_event_id")
        if last_event_id is not None and not isinstance(last_event_id, str):
            return _error(400, "invalid_payload", "last_event_id must be a string")
        result = store.put_sync_cursor(
            client_id=client_id, last_seq=last_seq, last_event_id=last_event_id,
        )
        return jsonify(result), 200

    # -------------------------------------------------- sanitized errors #
    @app.errorhandler(404)
    def _not_found(_exc):
        return _error(404, "not_found", "not found")

    @app.errorhandler(405)
    def _method_not_allowed(_exc):
        return _error(405, "method_not_allowed", "method not allowed")

    @app.errorhandler(413)
    def _payload_too_large(_exc):
        return _error(413, "payload_too_large", "request body too large")

    @app.errorhandler(Exception)
    def _unhandled(_exc):
        app.logger.exception("unhandled error")
        return _error(500, "internal_error", "internal error")

    return app


# --------------------------------------------------------------------------- #
# Helpers (module-level, no request/store state)
# --------------------------------------------------------------------------- #
def _parse_keys(raw: Optional[str]) -> Tuple[str, ...]:
    """Split the configured key(s) into a tuple; a comma-separated list is a
    rotation-overlap window (any listed key authenticates)."""
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _authorized(header: str, expected: Tuple[str, ...]) -> bool:
    """Constant-time bearer check against every accepted key. Fail closed if none
    is configured. Does NOT short-circuit on the first match: every key is
    compared so acceptance timing doesn't reveal which key matched."""
    if not expected:
        return False
    if not header.startswith(_BEARER_PREFIX):
        return False
    presented = header[len(_BEARER_PREFIX):].strip()
    if not presented:
        return False
    matched = False
    for key in expected:
        if hmac.compare_digest(presented, key):
            matched = True
    return matched


def _error(status: int, code: str, message: str) -> Tuple[Any, int]:
    """Uniform error envelope: ``{"error": {"code", "message"}}`` on every path.

    The structured ``code`` gives clients a stable, machine-readable discriminator
    (a future 409 version-conflict can carry its own code + fields) while
    ``message`` stays human-facing and sanitized (never a stack trace / SQL / DSN).
    """
    return jsonify({"error": {"code": code, "message": message}}), status


def _json_object() -> Tuple[Any, Optional[Tuple[Any, int]]]:
    """Parse the request body as a JSON object.

    Returns ``(data, None)`` on success or ``({}, error_response)`` when the body
    is absent or not a JSON object — the shared front half of every register route.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {}, _error(400, "invalid_payload", "expected a JSON object")
    return data, None


def _parse_after_seq(value: Optional[str]) -> Optional[int]:
    """Parse the ``after_seq`` keyset cursor; a bad/absent value means 'from newest'."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_limit(value: Optional[str]) -> int:
    try:
        limit = int(value) if value is not None else _RECENT_LIMIT_DEFAULT
    except (TypeError, ValueError):
        return _RECENT_LIMIT_DEFAULT
    if limit < 1:
        return 1
    return min(limit, _RECENT_LIMIT_MAX)


def _validation_detail(exc: ValidationError) -> str:
    """A compact, client-payload-only summary of a pydantic validation failure."""
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))
    return "; ".join(parts)[:500]


def _compact_json(item: Any) -> str:
    try:
        raw = json.dumps(item, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        raw = str(item)
    return raw[:_MAX_RAW_BYTES]


def _prepare_event(item: Any) -> PreparedEvent:
    """Validate one batch item into a :class:`PreparedEvent` (never raises)."""
    raw = _compact_json(item)
    if not isinstance(item, dict):
        return PreparedEvent(envelope=None, raw=raw, error="event must be a JSON object",
                             source_machine_id=None)
    machine_id = item.get("machine_id")
    source_machine_id = machine_id if isinstance(machine_id, str) else None
    try:
        envelope = EventEnvelope.model_validate(item)
    except ValidationError as exc:
        return PreparedEvent(envelope=None, raw=raw,
                             error=f"invalid envelope: {_validation_detail(exc)}",
                             source_machine_id=source_machine_id)
    return PreparedEvent(envelope=envelope, raw=raw, error=None,
                         source_machine_id=source_machine_id)


__all__ = ["create_app"]
