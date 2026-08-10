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

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

from flask import Flask, g, jsonify, request
from pydantic import ValidationError

from contracts.events import EventEnvelope
from contracts.identifiers import MEMORY, generate_ulid, new_id
from contracts.identity import MachineIdentity, RepositoryIdentity
from contracts.registry import AgentRegistration
from memory_core.adapters import load_content
from memory_core.availability import MemoryUnavailable, check_availability

from .store import ALLOWED_STATUSES, PreparedEvent

_API_KEY_ENV = "AGENT_OS_API_KEY"
_BEARER_PREFIX = "Bearer "
# Per-machine credential key_id prefix ('key_' + ULID). Reuses the contracts
# prefixed-ULID scheme via generate_ulid without registering a new global prefix:
# a key_id is a server-side auth artifact resolved by DB lookup, never validated
# through contracts.is_valid_id. token_urlsafe secrets never contain '.', so the
# presented '<key_id>.<secret>' token splits unambiguously on the first '.'.
_KEY_ID_PREFIX = "key_"
_KEY_SECRET_BYTES = 32  # secrets.token_urlsafe entropy for the secret half
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

# memory.documents.source_path is NVARCHAR(1000); reject an oversized identity key
# at the app layer with a clean 400 rather than a driver truncation 500 (mirrors
# the client_id guard above). Content itself is bounded by MAX_CONTENT_LENGTH.
_MAX_SOURCE_PATH_CHARS = 1000

# ops.sync_cursors.last_seq is a SQL BIGINT (see database/migrations/005). Reject
# an out-of-range value (negative, or beyond BIGINT max) at the app layer with a
# clean 400: a negative value would otherwise be accepted and later make a
# --follow keyset walk treat it as a valid-but-ancient cursor, replaying the
# entire event history up to its max_pages bound (code review, card 93baf05b);
# an overflowing value would instead reach the SQL Server driver and surface as
# a generic sanitized 500 rather than a client-correctable 400.
_MIN_LAST_SEQ = 0
_MAX_LAST_SEQ = 9223372036854775807  # SQL Server BIGINT max


@dataclass(frozen=True)
class AuthContext:
    """The resolved caller identity for one request, stored on ``flask.g.auth``.

    ``is_admin`` marks a holder of the shared ``AGENT_OS_API_KEY`` (bootstrap /
    key-issuance authority; bypasses machine binding). A per-machine credential
    resolves to ``is_admin=False`` with ``machine_id`` set to the machine the key
    is bound to (and ``key_id`` for telemetry), so a handler can require that a
    body's ``machine_id`` matches the caller.
    """

    is_admin: bool
    machine_id: Optional[str] = None
    key_id: Optional[str] = None


def create_app(
    *,
    api_key: Optional[str] = None,
    store: Optional[Any] = None,
    memory_store: Optional[Any] = None,
    memory_embedder: Optional[Callable[[], Any]] = None,
) -> Flask:
    """Build the Agent OS API Flask app.

    ``api_key`` defaults to the ``AGENT_OS_API_KEY`` environment variable. It may
    be a single key OR a comma-separated LIST of keys — every listed key is
    accepted, so a key can be rotated with an overlap window (add the new key,
    redeploy clients, then drop the old). If it resolves to empty, every ``/v1``
    request fails closed with 401 (a server with no key configured can
    authenticate nobody). ``store`` defaults to a
    :class:`~services.api.store.SqlStore` over :func:`database.migrate.connect`;
    tests inject a :class:`~services.api.store.FakeStore`.

    ``memory_store`` / ``memory_embedder`` back ``POST /v1/memory/ingest`` and are
    injected the same way as ``store``: ``memory_store`` defaults to a
    :class:`~services.api.memory_store.MemoryStore` over
    :func:`database.migrate.connect`, and ``memory_embedder`` is a zero-arg factory
    resolving the hub-side embedder (default: availability-gated bge-small). Tests
    inject a :class:`~services.api.memory_store.FakeMemoryStore` + a deterministic
    fake embedder factory so ingest runs with no SQL Server and no real model.
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

    if memory_store is None:
        from database import migrate

        from .memory_store import MemoryStore

        memory_store = MemoryStore(migrate.connect)
    if memory_embedder is None:
        memory_embedder = _default_embedder_factory

    # ----------------------------------------------------------------- auth #
    @app.before_request
    def _require_auth():
        if request.path == "/health":
            return None
        header = request.headers.get("Authorization", "")
        auth = _resolve_auth(header, accepted_keys, store)
        if auth is None:
            return _error(401, "unauthorized", "unauthorized")
        # Publish the resolved identity for the handlers (admin vs. bound machine).
        g.auth = auth
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
        bind_err = _require_machine_match(ident.machine_id)
        if bind_err is not None:
            return bind_err
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
        bind_err = _require_machine_match(ident.machine_id)
        if bind_err is not None:
            return bind_err
        owner_machine_id = _caller_owner_machine_id()
        result = store.upsert_repository(
            repository_id=ident.repository_id, machine_id=ident.machine_id,
            repo_root=repo_root, canonical_remote=ident.canonical_remote,
            project_id=ident.project_id, created_at=ident.created_at,
            owner_machine_id=owner_machine_id,
        )
        # None => the repository_id exists under another machine (ownership guard).
        if result is None:
            return _error(403, "forbidden", "repository owned by another machine")
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
        bind_err = _require_machine_match(reg.machine_id)
        if bind_err is not None:
            return bind_err
        owner_machine_id = _caller_owner_machine_id()
        result = store.upsert_session(
            session_id=reg.session_id, agent_id=reg.agent_id, machine_id=reg.machine_id,
            repository_id=reg.repository_id, agent_type=reg.agent_type,
            parent_agent_id=reg.parent_agent_id, capabilities=list(reg.capabilities),
            current_task_id=reg.current_task_id, status=reg.status.value,
            model_runtime=reg.runtime, started_at=reg.started_at,
            heartbeat_at=reg.heartbeat_at, expires_at=reg.expected_expiration_at,
            owner_machine_id=owner_machine_id,
        )
        # None => the session_id exists under another machine (ownership guard).
        if result is None:
            return _error(403, "forbidden", "session owned by another machine")
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
        # Machine binding: a per-machine caller may heartbeat ONLY the sessions it
        # owns. Enforced at the store layer against the session's authoritative
        # owning machine_id (NOT the spoofable/omittable body machine_id) — a
        # cross-machine target matches 0 rows and returns 404. Admin bypasses.
        owner_machine_id = _caller_owner_machine_id()
        result = store.heartbeat_session(
            session_id=session_id, status=status, current_task_id=current_task_id,
            owner_machine_id=owner_machine_id,
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
        # Machine binding: a per-machine caller may only submit events originating
        # on its OWN machine. A mismatched event is turned into a per-event
        # ``rejected`` (recorded in ops.ingest_errors, same as a malformed item) so
        # one foreign event never fails the whole batch — mirroring the existing
        # per-event verdict style. Admin callers bypass (is_admin).
        auth = getattr(g, "auth", None)
        if auth is not None and not auth.is_admin:
            prepared = [_bind_event(item, auth.machine_id) for item in prepared]
        try:
            outcomes = store.ingest_events(prepared)
        except Exception:
            # A transport / DB failure — return 5xx WITHOUT a per-event verdict so
            # the drain does not advance its cursor. Log server-side; never leak the
            # cause to the client.
            app.logger.exception("event ingest failed")
            return _error(500, "internal_error", "event ingestion failed")
        return jsonify({"results": outcomes}), 200

    # ------------------------------------------------------- memory ingest #
    @app.post("/v1/memory/ingest")
    def ingest_memory():
        """Ingest raw document content into the central memory corpus (WRITE path).

        The hub chunks → embeds (hub-side) → dedups → INSERTs into
        ``memory.documents`` / ``memory.chunks`` (migration 008). Behind
        ``_require_auth`` (memory is private — no unauthenticated route).
        Availability-gated: if the memory extras/model are absent the caller gets
        a clean 503, never a 500/crash. SYNCHRONOUS — the summary is returned once
        the write commits, mirroring the local ``memory_ingest`` tool result so a
        future client keeps an unchanged UX.
        """
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return _error(400, "invalid_payload", "expected a JSON object")
        content = data.get("content")
        if not isinstance(content, str) or not content.strip():
            return _error(400, "invalid_payload", "content is required")
        source_type = data.get("source_type")
        if source_type is not None and not isinstance(source_type, str):
            return _error(400, "invalid_payload", "source_type must be a string")
        source_path = data.get("source_path")
        if source_path is not None:
            if not isinstance(source_path, str):
                return _error(400, "invalid_payload", "source_path must be a string")
            if len(source_path) > _MAX_SOURCE_PATH_CHARS:
                return _error(400, "invalid_payload", "source_path is too long")
        published_at = data.get("published_at")
        if published_at is not None and not isinstance(published_at, str):
            return _error(400, "invalid_payload", "published_at must be a string")

        # Build SourceDocuments (adapter-detect ChatGPT export vs prose) via the
        # SAME memory_core code the local ingest uses. A bad source_type, a
        # missing published_at for news, or a bad ISO date is a client error → 400.
        try:
            docs = load_content(
                content,
                source_type=source_type,
                source_path=source_path or None,
                published_at=published_at,
                mint_doc_id=lambda: new_id(MEMORY),
            )
        except ValueError as exc:
            return _error(400, "invalid_payload", str(exc))

        # Availability gate: resolve the hub-side embedder. Absent extras/model →
        # a clean 503 (mirrors the memory_* MCP tools' unavailable result).
        try:
            embedder = memory_embedder()
        except MemoryUnavailable:
            return _error(503, "memory_unavailable", "memory subsystem unavailable")

        # Provenance: a per-machine caller's OWN machine (authoritative, from the
        # resolved credential); an admin may name one in the body, or None.
        auth = g.auth
        if auth.is_admin:
            body_machine = data.get("source_machine_id")
            source_machine_id = body_machine if isinstance(body_machine, str) else None
        else:
            source_machine_id = auth.machine_id

        try:
            summary = memory_store.ingest_documents(
                docs, embedder, source_machine_id=source_machine_id
            )
        except MemoryUnavailable:
            return _error(503, "memory_unavailable", "memory subsystem unavailable")
        except Exception:
            app.logger.exception("memory ingest failed")
            return _error(500, "internal_error", "memory ingestion failed")

        return jsonify({
            "available": True,
            "operation": "memory_ingest",
            "documents_detected": len(docs),
            **summary,
        }), 200

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

    # --------------------------------------------------- task down-read #
    @app.get("/v1/tasks")
    def list_tasks():
        """Central task projections for DOWN-synchronization (Phase 2a).

        Auth-required (via ``_require_auth``); mirrors ``/v1/events/recent`` but
        pages FORWARD over the mutable registry.tasks read model by its
        ROWVERSION-derived ``change_seq``: ``?since=`` fetches strictly-newer
        changes and ``next_since`` is the continuation token (the largest
        ``change_seq`` on this page, ``null`` when the caller is caught up). A
        down-projection poller filters ``?repository_id=`` to the source repo it
        is applying.
        """
        repository_id = request.args.get("repository_id") or None
        since = _parse_after_seq(request.args.get("since"))
        limit = _parse_limit(request.args.get("limit"))
        tasks = store.list_tasks(repository_id=repository_id, since=since, limit=limit)
        next_since = tasks[-1].get("change_seq") if tasks else None
        return jsonify({"tasks": tasks, "next_since": next_since}), 200

    # -------------------------------------------------------- sync cursors #
    @app.get("/v1/sync/<client_id>")
    def get_sync_cursor(client_id: str):
        # Admin-only stopgap: ops.sync_cursors has NO per-machine ownership column
        # yet, so a per-machine key must not read/overwrite another consumer's
        # replay position (security review round 3, Medium). Proper first-writer-
        # wins binding is a tracked follow-up to land BEFORE per-machine keys reach
        # sync consumers; today every sync consumer uses the shared admin key.
        admin_err = _require_admin()
        if admin_err is not None:
            return admin_err
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
        admin_err = _require_admin()  # admin-only stopgap; see get_sync_cursor
        if admin_err is not None:
            return admin_err
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

    # ---------------------------------------- key management (admin-only) #
    @app.post("/v1/machines/<machine_id>/credentials")
    def issue_credential(machine_id: str):
        """Issue a per-machine API key. ADMIN-ONLY. Returns the secret ONCE."""
        admin_err = _require_admin()
        if admin_err is not None:
            return admin_err
        if not store.machine_exists(machine_id):
            return _error(404, "not_found", "machine not found")
        data = request.get_json(silent=True)
        label = data.get("label") if isinstance(data, dict) else None
        if label is not None and not isinstance(label, str):
            label = None  # tolerant reader: ignore a non-string label
        key_id, secret, secret_hash = _mint_credential()
        store.insert_credential(
            key_id=key_id, machine_id=machine_id, secret_hash=secret_hash, label=label,
        )
        # The plaintext secret is returned exactly once and never stored — only its
        # SHA-256 hash is persisted, so it can never be shown or recovered again.
        return jsonify({
            "key_id": key_id, "secret": secret, "key": f"{key_id}.{secret}",
        }), 201

    @app.post("/v1/machines/<machine_id>/credentials/<key_id>/revoke")
    def revoke_credential(machine_id: str, key_id: str):
        """Revoke a per-machine API key (idempotent). ADMIN-ONLY."""
        admin_err = _require_admin()
        if admin_err is not None:
            return admin_err
        cred = store.get_credential(key_id)
        if cred is None or cred.get("machine_id") != machine_id:
            return _error(404, "not_found", "credential not found")
        store.revoke_credential(key_id)
        return jsonify({"key_id": key_id, "status": "revoked"}), 200

    @app.get("/v1/machines/<machine_id>/credentials")
    def list_credentials(machine_id: str):
        """List a machine's credentials (ids + status, never secrets). ADMIN-ONLY."""
        admin_err = _require_admin()
        if admin_err is not None:
            return admin_err
        return jsonify({"credentials": store.list_credentials(machine_id)}), 200

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


def _resolve_auth(
    header: str, expected: Tuple[str, ...], store: Any
) -> Optional[AuthContext]:
    """Two-phase bearer resolution → an :class:`AuthContext`, or ``None`` (401).

    Phase 1 (admin): the EXACT legacy :func:`_authorized` check over the configured
    ``AGENT_OS_API_KEY`` tuple — constant-time, non-short-circuit, fail-closed when
    unset, comma-separated rotation window. A match is the full-authority admin /
    bootstrap identity.

    Phase 2 (per-machine): only reached when phase 1 fails AND the presented token
    contains a ``.`` (the ``<key_id>.<secret>`` shape). The ``key_id`` is looked up
    O(1); the credential must exist and be un-revoked; and the SHA-256 of the
    presented secret must ``hmac.compare_digest``-match the stored hash. Only then
    is the caller the bound machine. On success ``last_used_at`` is refreshed
    best-effort (never fails auth). Anything else is unauthenticated.
    """
    # Phase 1 — admin key path, byte-for-byte the legacy behavior.
    if _authorized(header, expected):
        return AuthContext(is_admin=True)
    # Phase 2 — per-machine credential. Re-parse the (already validated) header.
    if not header.startswith(_BEARER_PREFIX):
        return None
    presented = header[len(_BEARER_PREFIX):].strip()
    if not presented or "." not in presented:
        return None
    key_id, _, secret = presented.partition(".")
    if not key_id or not secret:
        return None
    try:
        cred = store.get_credential(key_id)
    except Exception:
        # A store/DB failure during auth resolution must fail closed, not 500.
        return None
    if cred is None or cred.get("revoked_at") is not None:
        return None
    expected_hash = cred.get("secret_hash") or ""
    presented_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(presented_hash, expected_hash):
        return None
    try:
        store.touch_credential_last_used(key_id)
    except Exception:
        pass  # telemetry only — never fail an otherwise-valid auth
    return AuthContext(is_admin=False, machine_id=cred.get("machine_id"), key_id=key_id)


def _caller_owner_machine_id() -> Optional[str]:
    """The machine a state-changing handler must bind its target to: the caller's
    OWN machine for a per-machine key, or ``None`` for the admin/bootstrap key
    (unrestricted). Reads ``g.auth`` DIRECTLY (not ``getattr(..., None)``) so a
    handler somehow reached without a resolved auth context — which
    ``_require_auth`` prevents for every ``/v1`` route — raises and fails CLOSED
    (500) rather than silently defaulting to admin scope."""
    auth = g.auth
    return None if auth.is_admin else auth.machine_id


def _require_admin() -> Optional[Tuple[Any, int]]:
    """Return a 403 error response unless the current caller is the admin key."""
    auth = getattr(g, "auth", None)
    if auth is None or not auth.is_admin:
        return _error(403, "forbidden", "admin key required")
    return None


def _require_machine_match(body_machine_id: Optional[str]) -> Optional[Tuple[Any, int]]:
    """Enforce machine binding for a handler that writes/targets a ``machine_id``.

    Admin callers bypass (they are the bootstrap/issuance authority). A per-machine
    caller is allowed only when the operation's ``machine_id`` matches the machine
    the key is bound to; a mismatch is a 403. A ``None`` ``body_machine_id`` (e.g. a
    heartbeat that names no machine) imposes no binding — the mandatory-machine_id
    register routes always pass a real value here.
    """
    auth = getattr(g, "auth", None)
    if auth is None or auth.is_admin:
        return None
    if body_machine_id is not None and body_machine_id != auth.machine_id:
        return _error(403, "forbidden", "machine binding: key does not match machine_id")
    return None


def _default_embedder_factory() -> Any:
    """Resolve the hub-side default embedder, or raise MemoryUnavailable.

    Availability-gated exactly like the memory_* MCP tools (``check_availability``)
    then loads the memoized bge-small embedder. Called per ingest request but the
    embedder is process-cached by ``get_default_embedder``'s ``lru_cache``, so the
    model loads at most once per process. Heavy imports stay lazy (inside the
    function) so importing this module never pulls sentence-transformers/torch.
    """
    availability = check_availability()
    if not availability.get("available"):
        raise MemoryUnavailable(availability.get("hint") or "memory subsystem unavailable")
    from memory_core.embeddings import get_default_embedder

    return get_default_embedder()


def _mint_credential() -> Tuple[str, str, str]:
    """Mint ``(key_id, secret, secret_hash)`` for a new per-machine credential.

    ``key_id`` is a non-secret ``key_`` + ULID; ``secret`` is a high-entropy
    ``token_urlsafe`` value (never persisted); ``secret_hash`` is its SHA-256 hex
    digest (the ONLY thing stored).
    """
    key_id = _KEY_ID_PREFIX + generate_ulid()
    secret = secrets.token_urlsafe(_KEY_SECRET_BYTES)
    secret_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return key_id, secret, secret_hash


def _bind_event(item: PreparedEvent, caller_machine_id: Optional[str]) -> PreparedEvent:
    """Rebind one prepared event to a per-machine caller (events-ingest binding).

    An already-rejected (malformed) item passes through unchanged. A valid event
    whose originating ``machine_id`` differs from the caller's bound machine is
    converted into a ``rejected`` :class:`PreparedEvent`, so the batch's other
    events still ingest and the mismatch is recorded per-event in
    ``ops.ingest_errors`` — never a whole-batch failure.
    """
    if item.envelope is None:
        return item
    if item.source_machine_id != caller_machine_id:
        return PreparedEvent(
            envelope=None, raw=item.raw,
            error="machine binding: event machine_id does not match caller",
            source_machine_id=item.source_machine_id,
        )
    return item


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
