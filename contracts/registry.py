"""Agent session registration (proposal §11.3 / §5.2).

When an agent session starts it *registers*: it declares who it is, where it
runs, what it may do, and what it is currently working on, and it heartbeats to
prove liveness.  :class:`AgentRegistration` is that record — the contract a
future agent-registry service (Phase 1) will accept and store.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import AwareDatetime, Field

from .base import AgentOSModel, Dotted


class AgentStatus(str, Enum):
    """Coarse agent-session lifecycle state (aligns with the ``agent.*`` events)."""

    STARTING = "starting"
    RUNNING = "running"
    WAITING = "waiting"
    FAILED = "failed"
    FINISHED = "finished"


class AgentRegistration(AgentOSModel):
    """A registered agent session (proposal §11.3).

    Captures the identity/topology of the session (agent, session, machine,
    repository, parent agent), its declared ``capabilities``, the ``current_task``
    it is working on (if any), its ``status`` and last ``heartbeat_at``, its
    ``runtime`` (model/runtime string), and its ``started_at`` /
    ``expected_expiration_at`` bounds.
    """

    agent_id: str = Field(description="Prefixed ULID for the agent (agent_...).")
    session_id: str = Field(description="Prefixed ULID for the session (sess_...).")
    agent_type: str = Field(description="Specialist role, e.g. 'implementer'.")
    machine_id: str
    repository_id: Optional[str] = None
    parent_agent_id: Optional[str] = Field(
        default=None, description="Spawning agent's id, if this is a subagent."
    )
    capabilities: List[Dotted] = Field(
        default_factory=list,
        description=(
            "Declared capabilities as open dot-namespaced strings (Capability "
            "registry values today; unknown domain.verb strings accepted so a "
            "newer agent's registration still parses on an older build)."
        ),
    )
    current_task_id: Optional[str] = None
    status: AgentStatus = AgentStatus.STARTING
    runtime: Optional[str] = Field(
        default=None, description="Model / runtime identifier, e.g. 'claude-opus'."
    )
    started_at: AwareDatetime
    heartbeat_at: Optional[AwareDatetime] = Field(
        default=None, description="Last liveness heartbeat (UTC)."
    )
    expected_expiration_at: Optional[AwareDatetime] = Field(
        default=None, description="When the session is expected to expire (UTC)."
    )


__all__ = ["AgentStatus", "AgentRegistration"]
