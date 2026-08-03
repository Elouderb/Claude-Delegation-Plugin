"""Event envelope and event taxonomy (proposal §10.2 / §10.3).

The :class:`EventEnvelope` is the single durable wire format for everything that
happens in Agent OS — task lifecycle, agent lifecycle, repo/DB activity, memory
proposals, sync progress.  Its shape mirrors proposal §10.2 *exactly* (ruling
3): a required identity/type/time header, a set of *nullable context fields*
locating the event in the machine/agent/session/project/repository/task
hierarchy, an open ``payload``, and the inherited ``schema_version``.

See ``EVENTS.md`` for the design rationale, including why we keep domain field
names rather than adopting CloudEvents attribute names, and a mapping table for
a future CloudEvents bridge.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional

from pydantic import AwareDatetime, Field

from .base import AgentOSModel, Dotted


class EventType(str, Enum):
    """Initial event taxonomy (proposal §10.3) — the **producer-side registry**.

    This enum is the canonical, documented set of event types producers *should*
    emit; it is NOT the type of :attr:`EventEnvelope.event_type` (which is an
    open, dot-namespaced string — see below).  Keeping the taxonomy as a registry
    of constants gives producers a checked vocabulary and readers a reference,
    while the open field type means a consumer on an older contract build can
    still parse, route, and dead-letter an event whose type it does not yet
    recognise (a closed ``Enum`` field would fail to parse it, stalling an outbox
    drain).  Values are dotted ``domain.verb`` strings; the registry is additive.
    """

    # task.*
    TASK_CREATED = "task.created"
    TASK_UPDATED = "task.updated"
    TASK_CLAIMED = "task.claimed"
    TASK_BLOCKED = "task.blocked"
    TASK_COMPLETED = "task.completed"
    TASK_RECONCILED = "task.reconciled"  # §14.3 optimistic-concurrency reconciliation

    # agent.*
    AGENT_STARTED = "agent.started"
    AGENT_STATUS_CHANGED = "agent.status_changed"
    AGENT_WAITING = "agent.waiting"
    AGENT_FAILED = "agent.failed"
    AGENT_FINISHED = "agent.finished"

    # repo.*
    REPO_FILE_CHANGED = "repo.file_changed"
    REPO_GRAPH_UPDATED = "repo.graph_updated"
    REPO_COMMIT_CREATED = "repo.commit_created"
    REPO_TESTS_COMPLETED = "repo.tests_completed"

    # db.*
    DB_GRAPH_UPDATED = "db.graph_updated"

    # memory.*
    MEMORY_OBSERVATION_CREATED = "memory.observation_created"
    MEMORY_PROJECT_UPDATED = "memory.project_updated"
    MEMORY_PROMOTION_REQUESTED = "memory.promotion_requested"
    MEMORY_GLOBAL_UPDATED = "memory.global_updated"

    # sync.*
    SYNC_STARTED = "sync.started"
    SYNC_COMPLETED = "sync.completed"
    SYNC_FAILED = "sync.failed"


class EventEnvelope(AgentOSModel):
    """A single Agent OS event (proposal §10.2).

    Required header: ``event_id`` (``evt_`` ULID), ``event_type``,
    ``occurred_at`` (UTC), and ``machine_id`` (every event originates on a
    machine).  The remaining context fields are nullable because not every event
    is scoped to an agent, session, project, repository, or task.
    """

    event_id: str = Field(description="Prefixed ULID, e.g. evt_01J...")
    event_type: Dotted = Field(
        description=(
            "Open dot-namespaced event type (e.g. 'task.completed'). Producers "
            "SHOULD use a value from the EventType registry; consumers accept any "
            "well-formed domain.verb string so unknown types stay routable."
        )
    )
    occurred_at: AwareDatetime = Field(
        description="When the event occurred (timezone-aware UTC ISO-8601)."
    )
    machine_id: str = Field(description="Originating machine id (mach_...).")

    # Nullable context fields — locate the event in the topology when known.
    agent_id: Optional[str] = None
    session_id: Optional[str] = None
    project_id: Optional[str] = None
    repository_id: Optional[str] = None
    task_id: Optional[str] = None

    payload: Dict[str, Any] = Field(
        default_factory=dict,
        description="Event-type-specific body; opaque to the envelope.",
    )


__all__ = ["EventType", "EventEnvelope"]
