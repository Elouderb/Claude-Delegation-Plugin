"""Persistence layer for the Agent OS API — a thin store behind the endpoints.

The Flask app talks to a :class:`Store` (an informal interface — the two
implementations below share a method set), never to pyodbc directly, for two
reasons:

* **Per-request connection discipline.** Each :class:`SqlStore` method opens ONE
  short-lived connection via :func:`database.migrate.connect` (which registers the
  DATETIMEOFFSET output converter), does its parameterized work in a single
  transaction, commits, and closes — mirroring the card-DB per-operation
  connection posture. One HTTP request calls exactly one store method, so a
  request maps to exactly one connection.
* **Testability.** :class:`FakeStore` implements the same surface in memory, so the
  endpoint tests exercise routing / auth / validation / outcome logic against a
  pure fake with NO database and NO SQL parsing (card test requirement: "Flask
  test client for all endpoints ... with a FAKE db layer").

Upsert semantics (the subtle parts):

* ``registry.machines`` / ``registry.agent_sessions`` upsert on their PRIMARY KEY
  (UPDATE-else-INSERT, race-safe: a concurrent INSERT that loses the PK race falls
  back to UPDATE).
* ``registry.repositories`` upserts on its NATURAL KEY ``(machine_id, repo_root)``
  — backed by the ``UQ_registry_repositories_natural`` unique index on
  ``(machine_id, repo_root_hash)``. A re-registration of the same checkout with a
  NEWLY-MINTED ``repository_id`` (the ``.agent-os`` re-mint case that hardening in
  card 6f83ab1f finding 8 anticipated) UPDATES the existing row and RETURNS ITS
  ORIGINAL ``repository_id`` as the canonical id — it does not insert a duplicate.
  The response flags ``remapped=true`` so the client can reconcile its local id.

Event ingestion is **set-based**: one ``SELECT`` over the batch's ids resolves
which are already present (``duplicate``), then a single ``INSERT … SELECT FROM
OPENJSON(…) WHERE NOT EXISTS`` ships every genuinely-new event in one round trip
(idempotent on ``event_id`` — a redelivered event is a ``duplicate``, never a
PK-violation batch poison — and de-duplicated within the batch too). A
well-formed-but-invalid envelope is ``rejected`` and recorded in
``ops.ingest_errors``. A genuine DB error on a *valid* event rolls the whole batch
back and propagates, so the API returns 5xx and the drain does NOT advance its
cursor (no data loss).

Session read model. ``agent.started`` / ``agent.finished`` events materialize a
``registry.agent_sessions`` row (started → ``running``; finished → ``finished`` +
``ended_at``) so ``/v1/sessions?active=1`` reflects the real agents, not only the
drain's own self-registration (§17 "list active agents"). This materialization
runs on a SEPARATE best-effort connection AFTER the event batch commits: the event
log has no FK to ``registry.*`` (an event must always be recordable), but
``agent_sessions`` does, so a session row for a not-yet-registered machine is
skipped rather than allowed to roll back — and poison — the FK-free event ingest.

Sync cursors (Phase 1d, card 93baf05b). ``ops.sync_cursors`` upserts on its
PRIMARY KEY ``client_id`` exactly like ``registry.machines`` — one row per
consumer, refreshed in place. ``last_seq`` (migration 005) is the AUTHORITATIVE
resume position: it is the ``ops.events.ingest_seq`` value a ``GET
/v1/events/recent?after_seq=`` walk should resume from, because that is the
column the read endpoint's keyset pagination actually orders by.
``last_event_id`` is kept alongside purely for human-readable diagnosis (which
event that position corresponds to) and is never used to compute a resume
point — see ``database/README.md`` for why the original ``last_event_id``-only
column could not drive keyset pagination on its own.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

# ``database`` / ``contracts`` resolve because this package is imported from the
# repo root (see services/api/__main__.py and the test-suite sys.path convention).
from database.events import to_utc
from contracts.tasks import local_status_to_sync

# Allowed agent-session status values — mirrors contracts.registry.AgentStatus and
# the CK_registry_agent_sessions_status CHECK constraint. Kept here so the store
# (and the app's heartbeat validation) can reject an out-of-range status before it
# reaches the DB CHECK.
ALLOWED_STATUSES = ("starting", "running", "waiting", "failed", "finished")

# Session-lifecycle event types that materialize/close a registry.agent_sessions
# row (see the module docstring's "Session read model").
_AGENT_STARTED = "agent.started"
_AGENT_FINISHED = "agent.finished"

# Task-lifecycle event types that materialize a registry.tasks row (Phase 2a
# down-projection read model — see the "Task read model" note below). Only a
# task event carrying a ``global_id`` in its payload participates; a pre-Phase-2a
# task event (no global id) stays inert, exactly as before.
_TASK_CREATED = "task.created"
_TASK_UPDATED = "task.updated"
_TASK_COMPLETED = "task.completed"
_TASK_EVENT_TYPES = (_TASK_CREATED, _TASK_UPDATED, _TASK_COMPLETED)

# Max chars kept in ops.ingest_errors.reason (the column is NVARCHAR(1000)).
_MAX_REASON_CHARS = 1000

# Set-based batch ingest: OPENJSON-typed projection of one EventEnvelope row. The
# payload sub-object is extracted AS JSON (a JSON fragment, stored verbatim into
# the NVARCHAR(MAX) column); WHERE NOT EXISTS keeps the whole INSERT idempotent on
# event_id (a redelivered id is silently skipped, never a PK-violation poison).
_INSERT_EVENTS_OPENJSON_SQL = """
INSERT INTO ops.events
    (event_id, event_type, occurred_at, machine_id, agent_id, session_id,
     project_id, repository_id, task_id, payload, schema_version)
SELECT j.event_id, j.event_type, j.occurred_at, j.machine_id, j.agent_id,
       j.session_id, j.project_id, j.repository_id, j.task_id,
       COALESCE(j.payload, N'{}'), COALESCE(j.schema_version, 1)
FROM OPENJSON(?) WITH (
    event_id NVARCHAR(64) '$.event_id',
    event_type NVARCHAR(200) '$.event_type',
    occurred_at DATETIMEOFFSET '$.occurred_at',
    machine_id NVARCHAR(64) '$.machine_id',
    agent_id NVARCHAR(64) '$.agent_id',
    session_id NVARCHAR(64) '$.session_id',
    project_id NVARCHAR(64) '$.project_id',
    repository_id NVARCHAR(64) '$.repository_id',
    task_id NVARCHAR(64) '$.task_id',
    payload NVARCHAR(MAX) '$.payload' AS JSON,
    schema_version INT '$.schema_version'
) AS j
WHERE NOT EXISTS (SELECT 1 FROM ops.events e WHERE e.event_id = j.event_id)
"""

_EXISTING_EVENT_IDS_SQL = (
    "SELECT e.event_id FROM ops.events e JOIN OPENJSON(?) j ON j.value = e.event_id"
)


@dataclass(frozen=True)
class PreparedEvent:
    """One event from a ``/v1/events/batch`` body, pre-validated by the app.

    ``envelope`` is the parsed :class:`contracts.EventEnvelope` when the item is a
    valid envelope, else ``None`` with ``error`` set (the item is rejected and
    recorded in ``ops.ingest_errors``). ``raw`` is the compact JSON of the original
    item, preserved for the ingest-error record. ``source_machine_id`` is the item's
    ``machine_id`` when extractable (may be ``None`` for a malformed item).
    """

    envelope: Optional[Any]
    raw: str
    error: Optional[str]
    source_machine_id: Optional[str]


def _iso(value: Any) -> Any:
    """Serialize a datetime to an ISO-8601 string; pass anything else through."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


# ------------------------------------------------------------------------- #
# SQL-backed store (production)
# ------------------------------------------------------------------------- #
class SqlStore:
    """Central-DB-backed :class:`Store`. One connection per method call."""

    def __init__(self, connect: Callable[..., Any]):
        """``connect`` is a zero-arg callable returning a pyodbc connection with the
        DATETIMEOFFSET converter registered — normally ``database.migrate.connect``.
        """
        self._connect = connect

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _pyodbc_integrity_error() -> "type[BaseException]":
        import pyodbc  # local import: only needed when a driver error is caught

        return pyodbc.IntegrityError

    @staticmethod
    def _rows(cursor) -> List[Dict[str, Any]]:
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    # -- registry.machines ------------------------------------------------- #
    def upsert_machine(
        self, *, machine_id: str, name: str, os: str, created_at: datetime
    ) -> Dict[str, Any]:
        integrity_error = self._pyodbc_integrity_error()
        conn = self._connect()
        try:
            cur = conn.cursor()
            update_sql = (
                "UPDATE registry.machines SET name = ?, os = ?, "
                "last_seen_at = SYSDATETIMEOFFSET() WHERE machine_id = ?"
            )
            cur.execute(update_sql, name, os, machine_id)
            created = False
            if cur.rowcount == 0:
                try:
                    cur.execute(
                        "INSERT INTO registry.machines "
                        "(machine_id, name, os, created_at, last_seen_at) "
                        "VALUES (?, ?, ?, ?, SYSDATETIMEOFFSET())",
                        machine_id, name, os, to_utc(created_at),
                    )
                    created = True
                except integrity_error:
                    # Lost a concurrent INSERT race on the PK — fall back to UPDATE.
                    cur.execute(update_sql, name, os, machine_id)
            conn.commit()
            return {"machine_id": machine_id, "created": created}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- registry.repositories (natural-key upsert) ------------------------ #
    def upsert_repository(
        self,
        *,
        repository_id: str,
        machine_id: str,
        repo_root: str,
        canonical_remote: Optional[str],
        project_id: Optional[str],
        created_at: datetime,
    ) -> Dict[str, Any]:
        integrity_error = self._pyodbc_integrity_error()
        conn = self._connect()
        try:
            cur = conn.cursor()
            existing = self._find_repo_by_natural_key(cur, machine_id, repo_root)
            if existing is not None:
                self._update_repo(cur, existing, canonical_remote, project_id)
                conn.commit()
                return {
                    "repository_id": existing,
                    "created": False,
                    "remapped": existing != repository_id,
                }
            try:
                cur.execute(
                    "INSERT INTO registry.repositories "
                    "(repository_id, machine_id, canonical_remote, project_id, "
                    "repo_root, created_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, SYSDATETIMEOFFSET())",
                    repository_id, machine_id, canonical_remote, project_id,
                    repo_root, to_utc(created_at),
                )
                conn.commit()
                return {"repository_id": repository_id, "created": True, "remapped": False}
            except integrity_error:
                # The INSERT hit a constraint. Distinguish three cases:
                #  (a) a concurrent registration won the natural-key race — the
                #      natural key now resolves to the winner: reconcile to it.
                #  (b) the PK (repository_id) already exists under a STALE
                #      machine_id — this machine's identity was re-minted, but the
                #      repo identity file kept its original repository_id. Refresh
                #      that row's machine_id to the current one instead of looping
                #      on the constraint forever (finding: re-mint register loop).
                #  (c) a genuine constraint failure (e.g. the machine FK — machine
                #      truly not registered) with no such row either way: surface it.
                winner = self._find_repo_by_natural_key(cur, machine_id, repo_root)
                if winner is not None:
                    self._update_repo(cur, winner, canonical_remote, project_id)
                    conn.commit()
                    return {
                        "repository_id": winner,
                        "created": False,
                        "remapped": winner != repository_id,
                    }
                if self._repo_id_exists(cur, repository_id):
                    self._update_repo_machine(
                        cur, repository_id, machine_id, repo_root,
                        canonical_remote, project_id,
                    )
                    conn.commit()
                    return {
                        "repository_id": repository_id,
                        "created": False,
                        "remapped": False,
                    }
                raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _find_repo_by_natural_key(cur, machine_id: str, repo_root: str) -> Optional[str]:
        # Compare on the SAME expression the UQ_registry_repositories_natural index
        # is built from — a byte-exact SHA2_256 hash of repo_root — NOT a plain
        # ``repo_root = ?`` string equality. Under a case-insensitive DB collation
        # (this DB is SQL_Latin1_General_CP1_CI_AS) the string compare would match
        # two *different* checkouts that differ only by case (or trailing spaces),
        # silently cross-attributing one repo's events to another; the hash compare
        # matches iff the paths are byte-identical, exactly like the unique index.
        cur.execute(
            "SELECT repository_id FROM registry.repositories "
            "WHERE machine_id = ? "
            "AND repo_root_hash = CONVERT(BINARY(32), HASHBYTES('SHA2_256', ?))",
            machine_id, repo_root,
        )
        row = cur.fetchone()
        return row[0] if row is not None else None

    @staticmethod
    def _repo_id_exists(cur, repository_id: str) -> bool:
        cur.execute(
            "SELECT 1 FROM registry.repositories WHERE repository_id = ?",
            repository_id,
        )
        return cur.fetchone() is not None

    @staticmethod
    def _update_repo(
        cur, repository_id: str, canonical_remote: Optional[str], project_id: Optional[str]
    ) -> None:
        cur.execute(
            "UPDATE registry.repositories SET canonical_remote = ?, project_id = ?, "
            "last_seen_at = SYSDATETIMEOFFSET() WHERE repository_id = ?",
            canonical_remote, project_id, repository_id,
        )

    @staticmethod
    def _update_repo_machine(
        cur,
        repository_id: str,
        machine_id: str,
        repo_root: str,
        canonical_remote: Optional[str],
        project_id: Optional[str],
    ) -> None:
        """Re-point an existing repository row at the CURRENT machine identity.

        Used for the re-mint case: the checkout's ``repository_id`` already exists
        centrally but under a machine identity that was re-minted. Refreshing
        ``machine_id`` here is safe because the caller only reaches this path after
        confirming the natural key ``(machine_id, repo_root)`` is NOT already taken,
        so this UPDATE cannot collide with the unique index.
        """
        cur.execute(
            "UPDATE registry.repositories SET machine_id = ?, repo_root = ?, "
            "canonical_remote = ?, project_id = ?, last_seen_at = SYSDATETIMEOFFSET() "
            "WHERE repository_id = ?",
            machine_id, repo_root, canonical_remote, project_id, repository_id,
        )

    # -- registry.agent_sessions ------------------------------------------- #
    def upsert_session(
        self,
        *,
        session_id: str,
        agent_id: str,
        machine_id: str,
        repository_id: Optional[str],
        agent_type: str,
        parent_agent_id: Optional[str],
        capabilities: List[str],
        current_task_id: Optional[str],
        status: str,
        model_runtime: Optional[str],
        started_at: datetime,
        heartbeat_at: Optional[datetime],
        expires_at: Optional[datetime],
    ) -> Dict[str, Any]:
        integrity_error = self._pyodbc_integrity_error()
        caps_json = json.dumps(list(capabilities))
        hb = to_utc(heartbeat_at) if heartbeat_at is not None else None
        exp = to_utc(expires_at) if expires_at is not None else None
        conn = self._connect()
        try:
            cur = conn.cursor()
            update_sql = (
                "UPDATE registry.agent_sessions SET agent_id = ?, machine_id = ?, "
                "repository_id = ?, agent_type = ?, parent_agent_id = ?, status = ?, "
                "capabilities = ?, current_task_id = ?, model_runtime = ?, "
                "last_heartbeat_at = ?, expires_at = ? WHERE session_id = ?"
            )
            cur.execute(
                update_sql,
                agent_id, machine_id, repository_id, agent_type, parent_agent_id,
                status, caps_json, current_task_id, model_runtime, hb, exp, session_id,
            )
            created = False
            if cur.rowcount == 0:
                try:
                    cur.execute(
                        "INSERT INTO registry.agent_sessions "
                        "(session_id, agent_id, machine_id, repository_id, agent_type, "
                        "parent_agent_id, status, capabilities, current_task_id, "
                        "model_runtime, started_at, last_heartbeat_at, expires_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        session_id, agent_id, machine_id, repository_id, agent_type,
                        parent_agent_id, status, caps_json, current_task_id,
                        model_runtime, to_utc(started_at), hb, exp,
                    )
                    created = True
                except integrity_error:
                    cur.execute(
                        update_sql,
                        agent_id, machine_id, repository_id, agent_type, parent_agent_id,
                        status, caps_json, current_task_id, model_runtime, hb, exp,
                        session_id,
                    )
            conn.commit()
            return {"session_id": session_id, "created": created}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def heartbeat_session(
        self,
        *,
        session_id: str,
        status: Optional[str] = None,
        current_task_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE registry.agent_sessions SET last_heartbeat_at = SYSDATETIMEOFFSET(), "
                "status = COALESCE(?, status), current_task_id = COALESCE(?, current_task_id) "
                "WHERE session_id = ?",
                status, current_task_id, session_id,
            )
            if cur.rowcount == 0:
                conn.rollback()
                return None
            cur.execute(
                "SELECT last_heartbeat_at, status FROM registry.agent_sessions "
                "WHERE session_id = ?",
                session_id,
            )
            row = cur.fetchone()
            conn.commit()
            if row is None:  # pragma: no cover - just-updated row vanished
                return None
            return {"session_id": session_id, "heartbeat_at": _iso(row[0]), "status": row[1]}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- ops.events (set-based batch ingest) ------------------------------- #
    def ingest_events(self, prepared: List[PreparedEvent]) -> List[Dict[str, Any]]:
        """Ingest a batch with index-aligned per-event outcomes.

        Set-based, not per-event: one ``SELECT`` resolves which ids already exist
        (``duplicate``), then one ``INSERT … SELECT FROM OPENJSON`` ships all the
        new events in a single round trip — killing the old 500-RTT-per-batch cost
        while preserving exact ``accepted`` / ``duplicate`` / ``rejected`` verdicts
        (intra-batch repeats are de-duplicated too). Malformed items are recorded
        in ``ops.ingest_errors``. A DB failure on a valid event rolls the whole
        batch back and propagates (the app returns 5xx and the drain holds its
        cursor — at-least-once redelivery is idempotent on ``event_id``).
        """
        outcomes: List[Optional[Dict[str, Any]]] = [None] * len(prepared)
        rejected_rows: List[Tuple[Optional[str], str, str]] = []
        valid: List[Tuple[int, Any]] = []
        for index, item in enumerate(prepared):
            if item.envelope is None:
                reason = (item.error or "rejected")[:_MAX_REASON_CHARS]
                rejected_rows.append((item.source_machine_id, reason, item.raw))
                outcomes[index] = {"index": index, "outcome": "rejected", "reason": item.error}
            else:
                valid.append((index, item.envelope))

        inserted_envelopes: List[Any] = []
        conn = self._connect()
        try:
            cur = conn.cursor()
            existing = self._existing_event_ids(cur, [e.event_id for _, e in valid])
            seen: set = set()
            to_insert: List[Any] = []
            for index, env in valid:
                eid = env.event_id
                if eid in existing or eid in seen:
                    outcomes[index] = {"index": index, "event_id": eid, "outcome": "duplicate"}
                else:
                    seen.add(eid)
                    to_insert.append(env)
                    outcomes[index] = {"index": index, "event_id": eid, "outcome": "accepted"}
            if rejected_rows:
                cur.executemany(
                    "INSERT INTO ops.ingest_errors (source_machine_id, reason, raw) "
                    "VALUES (?, ?, ?)",
                    rejected_rows,
                )
            if to_insert:
                cur.execute(_INSERT_EVENTS_OPENJSON_SQL, _events_json(to_insert))
            conn.commit()
            inserted_envelopes = to_insert
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        # Derived read-model update AFTER the event log commits — see the module
        # docstring: the event log has no FK to registry.*, so it must never be
        # blocked by a session row whose machine/repo isn't registered.
        self._materialize_sessions(inserted_envelopes)
        # Task down-projection read model (Phase 2a) — same posture as sessions:
        # a SEPARATE best-effort pass on its own connection, after the FK-free
        # event log has committed.
        self._materialize_tasks(inserted_envelopes)
        return [o for o in outcomes if o is not None]

    @staticmethod
    def _existing_event_ids(cur, event_ids: List[str]) -> set:
        """Return the subset of ``event_ids`` already present in ops.events (one query)."""
        if not event_ids:
            return set()
        cur.execute(_EXISTING_EVENT_IDS_SQL, json.dumps(list(event_ids)))
        return {row[0] for row in cur.fetchall()}

    def _materialize_sessions(self, envelopes: List[Any]) -> None:
        """Upsert/close registry.agent_sessions rows from accepted agent.* events.

        Best-effort and per-event isolated: each session write is its own tiny
        transaction so an FK miss (a session for a machine/repository not yet
        registered) is rolled back and skipped without affecting the others or the
        already-committed event log. Only ``agent.started`` / ``agent.finished``
        events carrying a ``session_id`` participate.
        """
        session_events = [
            e for e in envelopes
            if e.event_type in (_AGENT_STARTED, _AGENT_FINISHED) and e.session_id
        ]
        if not session_events:
            return
        conn = self._connect()
        try:
            cur = conn.cursor()
            for env in session_events:
                try:
                    if env.event_type == _AGENT_STARTED:
                        self._materialize_session_started(cur, env)
                    else:
                        self._materialize_session_finished(cur, env)
                    conn.commit()
                except Exception:
                    conn.rollback()  # skip this session; never fail the ingest
        finally:
            conn.close()

    @staticmethod
    def _materialize_session_started(cur, env: Any) -> None:
        payload = env.payload or {}
        agent_type = payload.get("agent_type") or "agent"
        caps_json = json.dumps(payload.get("capabilities") or [])
        started = to_utc(env.occurred_at)
        cur.execute(
            "UPDATE registry.agent_sessions SET agent_id = ?, machine_id = ?, "
            "repository_id = ?, agent_type = ?, parent_agent_id = ?, status = 'running', "
            "capabilities = ?, current_task_id = ?, model_runtime = ?, "
            "last_heartbeat_at = ? WHERE session_id = ?",
            env.agent_id, env.machine_id, env.repository_id, agent_type,
            payload.get("parent_agent_id"), caps_json, env.task_id,
            payload.get("runtime"), started, env.session_id,
        )
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO registry.agent_sessions "
                "(session_id, agent_id, machine_id, repository_id, agent_type, "
                "parent_agent_id, status, capabilities, current_task_id, "
                "model_runtime, started_at, last_heartbeat_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)",
                env.session_id, env.agent_id, env.machine_id, env.repository_id,
                agent_type, payload.get("parent_agent_id"), caps_json, env.task_id,
                payload.get("runtime"), started, started,
            )

    @staticmethod
    def _materialize_session_finished(cur, env: Any) -> None:
        ended = to_utc(env.occurred_at)
        cur.execute(
            "UPDATE registry.agent_sessions SET status = 'finished', ended_at = ?, "
            "last_heartbeat_at = ? WHERE session_id = ?",
            ended, ended, env.session_id,
        )
        if cur.rowcount == 0:
            # agent.finished arrived without a materialized start (out of order, or
            # the start's FK was missing). Record a minimal finished row.
            payload = env.payload or {}
            cur.execute(
                "INSERT INTO registry.agent_sessions "
                "(session_id, agent_id, machine_id, repository_id, agent_type, "
                "status, capabilities, started_at, last_heartbeat_at, ended_at) "
                "VALUES (?, ?, ?, ?, ?, 'finished', N'[]', ?, ?, ?)",
                env.session_id, env.agent_id, env.machine_id, env.repository_id,
                payload.get("agent_type") or "agent", ended, ended, ended,
            )

    # -- task read model (Phase 2a down-projection) ------------------------ #
    def _materialize_tasks(self, envelopes: List[Any]) -> None:
        """Upsert registry.tasks rows from accepted task.* events (Phase 2a).

        Mirrors :meth:`_materialize_sessions`: best-effort and per-event isolated
        (each task write is its own tiny transaction) so a bad row is rolled back
        and skipped without poisoning the others or the already-committed event
        log. Only ``task.created`` / ``task.updated`` / ``task.completed`` events
        carrying a ``global_id`` payload participate.
        """
        task_events = [
            e for e in envelopes
            if e.event_type in _TASK_EVENT_TYPES and (e.payload or {}).get("global_id")
        ]
        if not task_events:
            return
        conn = self._connect()
        try:
            cur = conn.cursor()
            for env in task_events:
                try:
                    self._materialize_task(cur, env)
                    conn.commit()
                except Exception:
                    conn.rollback()  # skip this task; never fail the ingest
        finally:
            conn.close()

    @staticmethod
    def _materialize_task(cur, env: Any) -> None:
        """Upsert one registry.tasks row from a task.* event (last-write-wins).

        The central task carries the reconciliation metadata the NEXT slice needs
        (version / updated_at / originating_client / repository_id /
        last_status_event_id) but this slice resolves conflicts by simple
        last-write-wins: the UPDATE only lands when the incoming ``version`` is
        ``>=`` the stored one, so an out-of-order redelivery of an older event can
        never clobber a newer materialized state, and an equal-version redelivery
        re-applies identical content (idempotent). ``status`` is stored as the
        normalized TaskSyncStatus value so the row round-trips a TaskRecord.
        """
        payload = env.payload or {}
        global_id = payload.get("global_id")
        if not global_id:
            return
        version = int(payload.get("version") or 1)
        status = local_status_to_sync(payload.get("status"))
        updated = to_utc(env.occurred_at)
        cur.execute(
            "UPDATE registry.tasks SET local_card_id = ?, repository_id = ?, "
            "project_id = ?, title = ?, description = ?, priority = ?, status = ?, "
            "version = ?, originating_client = ?, updated_at = ?, "
            "last_status_event_id = ? WHERE global_id = ? AND ? >= version",
            payload.get("card_id"), env.repository_id, env.project_id,
            payload.get("title"), payload.get("description"), payload.get("priority"),
            status, version, env.machine_id, updated, env.event_id,
            global_id, version,
        )
        if cur.rowcount == 0:
            # rowcount 0 means either the row does not exist yet, or an older/equal
            # event lost the version guard against a newer stored state. INSERT only
            # when genuinely absent; otherwise the stored (newer) row wins.
            cur.execute("SELECT 1 FROM registry.tasks WHERE global_id = ?", global_id)
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO registry.tasks (global_id, local_card_id, repository_id, "
                    "project_id, title, description, priority, status, version, "
                    "originating_client, updated_at, last_status_event_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    global_id, payload.get("card_id"), env.repository_id, env.project_id,
                    payload.get("title"), payload.get("description"), payload.get("priority"),
                    status, version, env.machine_id, updated, env.event_id,
                )

    def list_tasks(
        self,
        *,
        repository_id: Optional[str] = None,
        since: Optional[int] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Tasks in change order by ``change_seq`` (a ROWVERSION-derived total order).

        ``change_seq`` (``CONVERT(BIGINT, registry.tasks.change_seq)``) advances on
        every upsert of a row — including an in-place UPDATE — so, unlike an
        insert-only IDENTITY, it drives a keyset cursor over a MUTABLE read model:
        pass ``since`` = the largest ``change_seq`` from the previous page to fetch
        strictly-newer changes. Ordered ASC (oldest change first) so a poller walks
        forward with ``since``; each row carries its ``change_seq`` so the poller
        can advance the cursor.
        """
        conn = self._connect()
        try:
            cur = conn.cursor()
            where: List[str] = []
            params: List[Any] = [int(limit)]
            if since is not None:
                # rowversion compares as big-endian binary(8); casting the bigint
                # cursor back to binary(8) keeps the predicate sargable (no function
                # on the indexed column).
                where.append("change_seq > CONVERT(BINARY(8), CAST(? AS BIGINT))")
                params.append(int(since))
            if repository_id is not None:
                where.append("repository_id = ?")
                params.append(repository_id)
            clause = (" WHERE " + " AND ".join(where)) if where else ""
            cur.execute(
                "SELECT TOP (?) global_id, local_card_id, repository_id, project_id, "
                "title, description, priority, status, status_reason, version, "
                "originating_client, updated_at, last_status_event_id, "
                "CONVERT(BIGINT, change_seq) AS change_seq "
                "FROM registry.tasks" + clause + " ORDER BY change_seq ASC",
                *params,
            )
            return [_serialize_row(r) for r in self._rows(cur)]
        finally:
            conn.close()

    # -- read surface ------------------------------------------------------ #
    def list_machines(self) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT machine_id, name, os, created_at, registered_at, last_seen_at "
                "FROM registry.machines ORDER BY registered_at DESC"
            )
            return [_serialize_row(r) for r in self._rows(cur)]
        finally:
            conn.close()

    def list_repositories(self, machine_id: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            sql = (
                "SELECT repository_id, machine_id, canonical_remote, project_id, "
                "repo_root, created_at, registered_at, last_seen_at "
                "FROM registry.repositories"
            )
            params: List[Any] = []
            if machine_id is not None:
                sql += " WHERE machine_id = ?"
                params.append(machine_id)
            sql += " ORDER BY registered_at DESC"
            cur.execute(sql, *params)
            return [_serialize_row(r) for r in self._rows(cur)]
        finally:
            conn.close()

    def list_sessions(self, active: bool = False) -> List[Dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            sql = (
                "SELECT session_id, agent_id, machine_id, repository_id, agent_type, "
                "parent_agent_id, status, capabilities, current_task_id, model_runtime, "
                "started_at, last_heartbeat_at, expires_at, ended_at "
                "FROM registry.agent_sessions"
            )
            if active:
                sql += " WHERE status IN ('starting', 'running', 'waiting')"
            sql += " ORDER BY started_at DESC"
            cur.execute(sql)
            rows = [_serialize_row(r) for r in self._rows(cur)]
            for row in rows:
                row["capabilities"] = _parse_json(row.get("capabilities"), default=[])
            return rows
        finally:
            conn.close()

    def recent_events(
        self,
        *,
        limit: int = 50,
        repository_id: Optional[str] = None,
        event_type: Optional[str] = None,
        after_seq: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Newest-first events by ``ingest_seq`` (a unique, monotonic total order).

        Ordering on ``ingest_seq`` (not ``occurred_at``, which has no unique
        tiebreaker) makes keyset pagination possible: pass ``after_seq`` = the
        smallest ``ingest_seq`` from the previous page to fetch strictly older
        rows. Each row carries its ``ingest_seq`` and ``received_at`` so the
        1d poller can drive that cursor.
        """
        conn = self._connect()
        try:
            cur = conn.cursor()
            where: List[str] = []
            params: List[Any] = [int(limit)]
            if after_seq is not None:
                where.append("ingest_seq < ?")
                params.append(int(after_seq))
            if repository_id is not None:
                where.append("repository_id = ?")
                params.append(repository_id)
            if event_type is not None:
                where.append("event_type = ?")
                params.append(event_type)
            clause = (" WHERE " + " AND ".join(where)) if where else ""
            cur.execute(
                "SELECT TOP (?) event_id, event_type, occurred_at, received_at, ingest_seq, "
                "machine_id, agent_id, session_id, project_id, repository_id, task_id, "
                "payload, schema_version FROM ops.events" + clause
                + " ORDER BY ingest_seq DESC",
                *params,
            )
            rows = [_serialize_row(r) for r in self._rows(cur)]
            for row in rows:
                row["payload"] = _parse_json(row.get("payload"), default={})
            return rows
        finally:
            conn.close()

    # -- ops.sync_cursors (Phase 1d) --------------------------------------- #
    def get_sync_cursor(self, *, client_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            return self._read_sync_cursor(cur, client_id)
        finally:
            conn.close()

    def put_sync_cursor(
        self, *, client_id: str, last_seq: Optional[int], last_event_id: Optional[str]
    ) -> Dict[str, Any]:
        integrity_error = self._pyodbc_integrity_error()
        conn = self._connect()
        try:
            cur = conn.cursor()
            update_sql = (
                "UPDATE ops.sync_cursors SET last_seq = ?, last_event_id = ?, "
                "updated_at = SYSDATETIMEOFFSET() WHERE client_id = ?"
            )
            cur.execute(update_sql, last_seq, last_event_id, client_id)
            if cur.rowcount == 0:
                try:
                    cur.execute(
                        "INSERT INTO ops.sync_cursors (client_id, last_seq, last_event_id) "
                        "VALUES (?, ?, ?)",
                        client_id, last_seq, last_event_id,
                    )
                except integrity_error:
                    # Lost a concurrent PUT race on the PK - fall back to UPDATE.
                    cur.execute(update_sql, last_seq, last_event_id, client_id)
            conn.commit()
            result = self._read_sync_cursor(cur, client_id)
            assert result is not None  # just written, on the same connection
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _read_sync_cursor(cur, client_id: str) -> Optional[Dict[str, Any]]:
        cur.execute(
            "SELECT client_id, last_seq, last_event_id, updated_at "
            "FROM ops.sync_cursors WHERE client_id = ?",
            client_id,
        )
        row = cur.fetchone()
        if row is None:
            return None
        columns = [d[0] for d in cur.description]
        return _serialize_row(dict(zip(columns, row)))


def _events_json(envelopes: List[Any]) -> str:
    """Serialize envelopes to the JSON array the OPENJSON batch INSERT consumes.

    ``occurred_at`` is UTC-normalized (:func:`database.events.to_utc`) so the
    stored instant is correct regardless of the sender's local offset; ``payload``
    is kept as a nested object so OPENJSON's ``AS JSON`` projection re-emits it as
    the JSON fragment stored in the ``NVARCHAR(MAX)`` column.
    """
    return json.dumps(
        [
            {
                "event_id": e.event_id,
                "event_type": e.event_type,
                "occurred_at": to_utc(e.occurred_at).isoformat(),
                "machine_id": e.machine_id,
                "agent_id": e.agent_id,
                "session_id": e.session_id,
                "project_id": e.project_id,
                "repository_id": e.repository_id,
                "task_id": e.task_id,
                "payload": e.payload,
                "schema_version": e.schema_version,
            }
            for e in envelopes
        ]
    )


def _serialize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Copy a row dict with every datetime value ISO-formatted (JSON-safe)."""
    return {key: _iso(value) for key, value in row.items()}


def _parse_json(value: Any, *, default: Any) -> Any:
    """Parse a JSON string column back into an object; tolerate a bad/absent value."""
    if not isinstance(value, str):
        return value if value is not None else default
    try:
        return json.loads(value)
    except ValueError:
        return default


# ------------------------------------------------------------------------- #
# In-memory store (tests)
# ------------------------------------------------------------------------- #
class FakeStore:
    """In-memory :class:`Store` for the endpoint tests (no database, no SQL).

    Mirrors :class:`SqlStore`'s method surface and upsert semantics (PK upsert for
    machines/sessions; natural-key ``(machine_id, repo_root)`` upsert for
    repositories that keeps the original ``repository_id``). ``raise_on_ingest``
    makes :meth:`ingest_events` raise, so the app's 5xx path can be exercised.
    """

    def __init__(self, *, raise_on_ingest: bool = False):
        self.machines: Dict[str, Dict[str, Any]] = {}
        self.repositories: Dict[str, Dict[str, Any]] = {}
        self._repo_natural: Dict[Tuple[str, str], str] = {}
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.event_ids: set = set()
        self.events: List[Dict[str, Any]] = []
        self.ingest_errors: List[Dict[str, Any]] = []
        self.raise_on_ingest = raise_on_ingest
        self._seq = 0  # mirrors ops.events.ingest_seq (monotonic arrival order)
        self.sync_cursors: Dict[str, Dict[str, Any]] = {}  # mirrors ops.sync_cursors
        self.tasks: Dict[str, Dict[str, Any]] = {}  # mirrors registry.tasks (by global_id)
        self._task_seq = 0  # mirrors registry.tasks.change_seq (ROWVERSION)

    def upsert_machine(self, *, machine_id, name, os, created_at) -> Dict[str, Any]:
        created = machine_id not in self.machines
        now = datetime.now(timezone.utc)
        record = self.machines.get(machine_id, {"created_at": _iso(created_at), "registered_at": _iso(now)})
        record.update({"machine_id": machine_id, "name": name, "os": os, "last_seen_at": _iso(now)})
        self.machines[machine_id] = record
        return {"machine_id": machine_id, "created": created}

    def upsert_repository(
        self, *, repository_id, machine_id, repo_root, canonical_remote, project_id, created_at
    ) -> Dict[str, Any]:
        key = (machine_id, repo_root)
        now = datetime.now(timezone.utc)
        existing = self._repo_natural.get(key)
        if existing is not None:
            record = self.repositories[existing]
            record.update(
                {"canonical_remote": canonical_remote, "project_id": project_id, "last_seen_at": _iso(now)}
            )
            return {"repository_id": existing, "created": False, "remapped": existing != repository_id}
        if repository_id in self.repositories:
            # PK exists under a STALE natural key: this machine's identity was
            # re-minted. Re-point the row at the current machine (mirrors SqlStore's
            # _update_repo_machine) rather than duplicating or looping.
            record = self.repositories[repository_id]
            old_machine, old_root = record.get("machine_id"), record.get("repo_root")
            if isinstance(old_machine, str) and isinstance(old_root, str):
                self._repo_natural.pop((old_machine, old_root), None)
            record.update({
                "machine_id": machine_id, "repo_root": repo_root,
                "canonical_remote": canonical_remote, "project_id": project_id,
                "last_seen_at": _iso(now),
            })
            self._repo_natural[key] = repository_id
            return {"repository_id": repository_id, "created": False, "remapped": False}
        self.repositories[repository_id] = {
            "repository_id": repository_id, "machine_id": machine_id,
            "canonical_remote": canonical_remote, "project_id": project_id,
            "repo_root": repo_root, "created_at": _iso(created_at),
            "registered_at": _iso(now), "last_seen_at": _iso(now),
        }
        self._repo_natural[key] = repository_id
        return {"repository_id": repository_id, "created": True, "remapped": False}

    def upsert_session(
        self, *, session_id, agent_id, machine_id, repository_id, agent_type,
        parent_agent_id, capabilities, current_task_id, status, model_runtime,
        started_at, heartbeat_at, expires_at,
    ) -> Dict[str, Any]:
        created = session_id not in self.sessions
        record = self.sessions.get(session_id, {"started_at": _iso(started_at), "ended_at": None})
        record.update({
            "session_id": session_id, "agent_id": agent_id, "machine_id": machine_id,
            "repository_id": repository_id, "agent_type": agent_type,
            "parent_agent_id": parent_agent_id, "status": status,
            "capabilities": list(capabilities), "current_task_id": current_task_id,
            "model_runtime": model_runtime,
            "last_heartbeat_at": _iso(heartbeat_at) if heartbeat_at else None,
            "expires_at": _iso(expires_at) if expires_at else None,
        })
        self.sessions[session_id] = record
        return {"session_id": session_id, "created": created}

    def heartbeat_session(self, *, session_id, status=None, current_task_id=None):
        record = self.sessions.get(session_id)
        if record is None:
            return None
        now = datetime.now(timezone.utc)
        record["last_heartbeat_at"] = _iso(now)
        if status is not None:
            record["status"] = status
        if current_task_id is not None:
            record["current_task_id"] = current_task_id
        return {"session_id": session_id, "heartbeat_at": _iso(now), "status": record["status"]}

    def ingest_events(self, prepared: List[PreparedEvent]) -> List[Dict[str, Any]]:
        if self.raise_on_ingest:
            raise RuntimeError("simulated DB failure")
        outcomes: List[Dict[str, Any]] = []
        accepted_envelopes: List[Any] = []
        seen: set = set()
        for index, item in enumerate(prepared):
            if item.envelope is None:
                # Mirror SqlStore's NVARCHAR(1000) reason truncation.
                reason = (item.error or "rejected")[:_MAX_REASON_CHARS]
                self.ingest_errors.append({"reason": reason, "raw": item.raw})
                outcomes.append({"index": index, "outcome": "rejected", "reason": item.error})
                continue
            event_id = item.envelope.event_id
            if event_id in self.event_ids or event_id in seen:
                outcomes.append({"index": index, "event_id": event_id, "outcome": "duplicate"})
                continue
            seen.add(event_id)
            self.event_ids.add(event_id)
            self._seq += 1
            record = item.envelope.model_dump(mode="json")
            record["ingest_seq"] = self._seq
            record["received_at"] = _iso(datetime.now(timezone.utc))
            self.events.append(record)
            accepted_envelopes.append(item.envelope)
            outcomes.append({"index": index, "event_id": event_id, "outcome": "accepted"})
        for env in accepted_envelopes:
            self._materialize_session(env)
            self._materialize_task(env)
        return outcomes

    def _materialize_task(self, env: Any) -> None:
        """Mirror SqlStore task materialization (Phase 2a) from task.* events.

        Same last-write-wins guard as SqlStore: an older-version event never
        clobbers a newer stored row; every accepted upsert bumps ``change_seq``
        (the ROWVERSION analogue) so the keyset read stays consistent with SQL.
        """
        if env.event_type not in _TASK_EVENT_TYPES:
            return
        payload = env.payload or {}
        global_id = payload.get("global_id")
        if not global_id:
            return
        version = int(payload.get("version") or 1)
        existing = self.tasks.get(global_id)
        if existing is not None and version < int(existing.get("version") or 1):
            return  # older event — do not clobber newer state (last-write-wins)
        self._task_seq += 1
        self.tasks[global_id] = {
            "global_id": global_id,
            "local_card_id": payload.get("card_id"),
            "repository_id": env.repository_id,
            "project_id": env.project_id,
            "title": payload.get("title"),
            "description": payload.get("description"),
            "priority": payload.get("priority"),
            "status": local_status_to_sync(payload.get("status")),
            "status_reason": None,
            "version": version,
            "originating_client": env.machine_id,
            "updated_at": _iso(env.occurred_at),
            "last_status_event_id": env.event_id,
            "change_seq": self._task_seq,
        }

    def _materialize_session(self, env: Any) -> None:
        """Mirror SqlStore session materialization from agent.started/finished."""
        if env.event_type not in (_AGENT_STARTED, _AGENT_FINISHED) or not env.session_id:
            return
        payload = env.payload or {}
        sid = env.session_id
        occurred = _iso(env.occurred_at)
        if env.event_type == _AGENT_STARTED:
            record = self.sessions.get(sid, {})
            record.update({
                "session_id": sid, "agent_id": env.agent_id, "machine_id": env.machine_id,
                "repository_id": env.repository_id,
                "agent_type": payload.get("agent_type") or "agent",
                "parent_agent_id": payload.get("parent_agent_id"), "status": "running",
                "capabilities": list(payload.get("capabilities") or []),
                "current_task_id": env.task_id, "model_runtime": payload.get("runtime"),
                "started_at": record.get("started_at", occurred),
                "last_heartbeat_at": occurred, "expires_at": record.get("expires_at"),
                "ended_at": None,
            })
            self.sessions[sid] = record
        else:  # agent.finished
            record = self.sessions.get(sid)
            if record is None:
                record = {
                    "session_id": sid, "agent_id": env.agent_id, "machine_id": env.machine_id,
                    "repository_id": env.repository_id,
                    "agent_type": payload.get("agent_type") or "agent",
                    "parent_agent_id": None, "capabilities": [], "current_task_id": env.task_id,
                    "model_runtime": None, "started_at": occurred, "expires_at": None,
                }
                self.sessions[sid] = record
            record["status"] = "finished"
            record["ended_at"] = occurred
            record["last_heartbeat_at"] = occurred

    def list_machines(self) -> List[Dict[str, Any]]:
        return list(self.machines.values())

    def list_repositories(self, machine_id: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = list(self.repositories.values())
        if machine_id is not None:
            rows = [r for r in rows if r.get("machine_id") == machine_id]
        return rows

    def list_sessions(self, active: bool = False) -> List[Dict[str, Any]]:
        rows = list(self.sessions.values())
        if active:
            rows = [r for r in rows if r.get("status") in ("starting", "running", "waiting")]
        return rows

    def recent_events(self, *, limit=50, repository_id=None, event_type=None, after_seq=None):
        # Mirror SqlStore: newest-first by the monotonic ingest_seq (not insertion
        # order), honoring the same after_seq keyset cursor.
        rows = sorted(self.events, key=lambda r: r["ingest_seq"], reverse=True)
        if after_seq is not None:
            rows = [r for r in rows if r["ingest_seq"] < int(after_seq)]
        if repository_id is not None:
            rows = [r for r in rows if r.get("repository_id") == repository_id]
        if event_type is not None:
            rows = [r for r in rows if r.get("event_type") == event_type]
        return rows[: int(limit)]

    def list_tasks(self, *, repository_id=None, since=None, limit=50):
        # Mirror SqlStore: oldest-change-first by the monotonic change_seq, honoring
        # the same `since` keyset cursor and optional repository filter.
        rows = sorted(self.tasks.values(), key=lambda r: r["change_seq"])
        if since is not None:
            rows = [r for r in rows if r["change_seq"] > int(since)]
        if repository_id is not None:
            rows = [r for r in rows if r.get("repository_id") == repository_id]
        return [dict(r) for r in rows[: int(limit)]]

    # -- ops.sync_cursors (Phase 1d) --------------------------------------- #
    def get_sync_cursor(self, *, client_id: str) -> Optional[Dict[str, Any]]:
        record = self.sync_cursors.get(client_id)
        return dict(record) if record is not None else None

    def put_sync_cursor(
        self, *, client_id: str, last_seq: Optional[int], last_event_id: Optional[str]
    ) -> Dict[str, Any]:
        record = {
            "client_id": client_id, "last_seq": last_seq, "last_event_id": last_event_id,
            "updated_at": _iso(datetime.now(timezone.utc)),
        }
        self.sync_cursors[client_id] = record
        return dict(record)


__all__ = ["ALLOWED_STATUSES", "PreparedEvent", "SqlStore", "FakeStore"]
