"""Round-trip a ``contracts.events.EventEnvelope`` through ``ops.events``.

Minimal by design: this phase ships the schema plus a verification round trip
(card 6f83ab1f item 4), not a real ingestion API — the outbox/API/sync worker
are later phases in epic 78a6ac11. These helpers exist so the live
verification test (and any future caller) has one obviously-correct
field <-> column mapping instead of re-deriving it ad hoc.

Depends on ``contracts`` (the shared, dependency-of-everything package) and
nothing in ``mcp/`` — same dependency direction as the rest of ``database/``.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from contracts.events import EventEnvelope  # noqa: E402

_EVENT_COLUMNS = (
    "event_id, event_type, occurred_at, machine_id, agent_id, session_id, "
    "project_id, repository_id, task_id, payload, schema_version"
)

_INSERT_EVENT_SQL = f"""
INSERT INTO ops.events ({_EVENT_COLUMNS})
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Idempotent insert: only inserts when no row with this event_id already exists,
# so a redelivered event (the NORMAL retry path — a crash between ack and cursor
# advance re-presents an already-delivered event) is a no-op instead of a PK
# violation that would poison a whole drain batch (card 6f83ab1f finding 5 /
# proposal §20). The trailing WHERE-NOT-EXISTS param is the event_id again.
_INSERT_EVENT_IF_ABSENT_SQL = f"""
INSERT INTO ops.events ({_EVENT_COLUMNS})
SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
WHERE NOT EXISTS (SELECT 1 FROM ops.events WHERE event_id = ?)
"""

_SELECT_EVENT_SQL = f"""
SELECT {_EVENT_COLUMNS}
FROM ops.events
WHERE event_id = ?
"""

_DELETE_EVENT_SQL = "DELETE FROM ops.events WHERE event_id = ?"


def to_utc(value: datetime) -> datetime:
    """Normalize an aware ``datetime`` to UTC before it touches a DATETIMEOFFSET column.

    Load-bearing driver fix (card 6f83ab1f finding 3): pyodbc binds an aware
    ``datetime`` to ``DATETIMEOFFSET`` by sending its wall-clock digits with a
    fixed ``+00:00`` offset — it does NOT carry the datetime's own offset — so a
    non-UTC value (e.g. ``12:00+05:30``) would be stored as ``12:00+00:00``, a
    different instant. Converting to UTC first makes the stored instant correct.
    Exported so the one canonical normalization is shared (``services.api.store``
    imports it) rather than re-derived per call site.
    """
    return value.astimezone(timezone.utc)


def _event_row_params(envelope: EventEnvelope) -> Tuple[Any, ...]:
    """Bind values for one ``ops.events`` row, in ``_EVENT_COLUMNS`` order.

    ``occurred_at`` is normalized to UTC (``.astimezone(timezone.utc)``) BEFORE
    binding. This is load-bearing: pyodbc binds an aware ``datetime`` to
    ``DATETIMEOFFSET`` by sending its wall-clock digits with a ``+00:00`` offset —
    it does NOT carry the datetime's own offset — so a non-UTC ``occurred_at``
    (e.g. ``12:00+05:30``) would otherwise be stored as ``12:00+00:00``, a
    different instant (verified live against this driver stack; card 6f83ab1f
    finding 3). Converting to UTC first makes the stored instant correct.
    ``payload`` is JSON-serialized (the column is ``NVARCHAR(MAX)``, not the
    native ``json`` type — see ``database/migrate.py`` module docstring).
    """
    return (
        envelope.event_id,
        envelope.event_type,
        to_utc(envelope.occurred_at),
        envelope.machine_id,
        envelope.agent_id,
        envelope.session_id,
        envelope.project_id,
        envelope.repository_id,
        envelope.task_id,
        json.dumps(envelope.payload),
        envelope.schema_version,
    )


def insert_event_envelope(conn: Any, envelope: EventEnvelope) -> None:
    """Insert one ``EventEnvelope`` into ``ops.events`` (unconditional).

    Does not commit — the caller controls the transaction boundary (mirrors
    ``database.migrate``'s per-caller-transaction discipline). Raises on a
    duplicate ``event_id`` (PK violation); use :func:`insert_event_if_absent` for
    the idempotent drain path.
    """
    cursor = conn.cursor()
    cursor.execute(_INSERT_EVENT_SQL, _event_row_params(envelope))


def insert_event_if_absent(conn: Any, envelope: EventEnvelope) -> bool:
    """Insert one ``EventEnvelope`` only if its ``event_id`` isn't already stored.

    Returns ``True`` if this call inserted the row, ``False`` if an event with the
    same ``event_id`` already existed (a duplicate redelivery — dropped, not
    double-recorded). Does not commit. This is the idempotent-delivery contract
    the Phase 1c drain relies on: ``EventEnvelope.model_validate_json(line)`` ->
    ``insert_event_if_absent(conn, envelope)`` -> advance cursor, with a
    crash-retry of an already-inserted event returning ``False`` harmlessly.
    """
    cursor = conn.cursor()
    params = _event_row_params(envelope) + (envelope.event_id,)
    cursor.execute(_INSERT_EVENT_IF_ABSENT_SQL, params)
    return cursor.rowcount == 1


def fetch_event_envelope(conn: Any, event_id: str) -> Optional[EventEnvelope]:
    """Read one ``ops.events`` row back and parse it as an ``EventEnvelope``.

    Returns ``None`` if no row with ``event_id`` exists. Requires the calling
    connection to have the DATETIMEOFFSET output converter registered (true
    of any connection from ``database.migrate.connect()``), otherwise
    fetching ``occurred_at``/``received_at`` raises.
    """
    cursor = conn.cursor()
    cursor.execute(_SELECT_EVENT_SQL, event_id)
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [d[0] for d in cursor.description]
    data = dict(zip(columns, row))
    data["payload"] = json.loads(data["payload"])
    return EventEnvelope.model_validate(data)


def delete_event(conn: Any, event_id: str) -> None:
    """Delete one ``ops.events`` row by id. Does not commit."""
    cursor = conn.cursor()
    cursor.execute(_DELETE_EVENT_SQL, event_id)


__all__ = [
    "to_utc",
    "insert_event_envelope",
    "insert_event_if_absent",
    "fetch_event_envelope",
    "delete_event",
]
