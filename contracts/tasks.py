"""Task synchronization contract (proposal §14).

:class:`TaskRecord` is the *synchronizable projection* of a task: the durable
mapping between a repository-local card and its central canonical identity, the
projectable content (title/description/priority/reasons — §14.1) needed to
render a central task back into a local card, plus the optimistic-concurrency
metadata needed to reconcile edits made on different machines.  Phase 0 defines
the shape only — there is **no** sync logic here (ruling 7); conflict resolution
and the outbox arrive in Phase 2.  The content fields are all optional: a record
may carry only identity + concurrency metadata (e.g. a status-only projection)
or the full renderable content.

Optimistic concurrency (proposal §14.3): ``version`` + ``updated_at`` +
``originating_client``, with ``last_status_event_id`` pointing at the immutable
status-history event that produced the current state.  Conflicting edits are
meant to yield a reconciliation event, never a silent overwrite.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import AwareDatetime, Field

from .base import AgentOSModel


class TaskSyncStatus(str, Enum):
    """Task status as carried across the sync boundary.

    Mirrors the local card lifecycle (Created -> In Progress -> Complete) plus an
    explicit ``blocked`` state that the distributed system needs to route around.
    """

    CREATED = "created"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETE = "complete"


class TaskRecord(AgentOSModel):
    """A task's synchronizable identity + concurrency metadata (proposal §14).

    ``global_id`` is the central canonical identity (``task_`` ULID);
    ``local_card_id`` is the existing short repository card id (e.g. ``ab12cd34``)
    and is nullable because a centrally-created task may not yet have a local
    projection.
    """

    global_id: str = Field(description="Central canonical task id (task_...).")
    local_card_id: Optional[str] = Field(
        default=None,
        description="Repository-local short card id, e.g. 'ab12cd34' (None until projected).",
    )
    project_id: Optional[str] = None
    repository_id: Optional[str] = None

    # Projectable content (proposal §14.1) — enough to render a central task back
    # into a local card.  All optional: a status-only projection may omit them.
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[str] = None

    status: TaskSyncStatus = TaskSyncStatus.CREATED
    status_reason: Optional[str] = Field(
        default=None, description="Why the task is in its current status (§14.1)."
    )
    blocked_reason: Optional[str] = Field(
        default=None, description="Why the task is blocked, when status is blocked (§14.1)."
    )

    # Optimistic-concurrency fields (proposal §14.3).
    version: int = Field(
        default=1, ge=1, description="Monotonic version for optimistic concurrency."
    )
    updated_at: AwareDatetime
    originating_client: str = Field(
        description="Id of the client/machine that produced this version."
    )
    last_status_event_id: Optional[str] = Field(
        default=None,
        description="Reference to the immutable status-history event (evt_...).",
    )


__all__ = ["TaskSyncStatus", "TaskRecord"]
