"""Context package contract (proposal §5.6).

The context compiler assembles a *bounded* task-specific context package rather
than an uncontrolled dump (proposal §5.6).  Phase 0 defines the shape and
validates it (ruling 8) — notably ``token_budget`` (a hard ceiling the compiler
must respect) and ``citations`` (so retrieved context is traceable to its
source).  Assembly logic itself is a later phase.

The graph/memory/event lists are intentionally ``list`` of open items: a package
may carry either bare node/event ids or embedded reference objects, and Phase 0
does not over-constrain that.  The scalar fields (files, constraints, citations,
budget) are typed tightly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import Field

from .base import AgentOSModel


class ContextPackage(AgentOSModel):
    """A bounded, cited context package for a task (proposal §5.6)."""

    task: Optional[Dict[str, Any]] = Field(
        default=None, description="The active task card / task artifact."
    )
    repo: Optional[Dict[str, Any]] = Field(
        default=None, description="Repository identity/summary."
    )
    relevant_files: List[str] = Field(default_factory=list)
    code_graph_nodes: List[Any] = Field(default_factory=list)
    db_graph_nodes: List[Any] = Field(default_factory=list)
    project_memories: List[Any] = Field(default_factory=list)
    global_memories: List[Any] = Field(default_factory=list)
    recent_events: List[Any] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    citations: List[str] = Field(
        default_factory=list,
        description="Source references making the package traceable.",
    )
    token_budget: int = Field(
        gt=0, description="Hard ceiling on assembled context size, in tokens."
    )


__all__ = ["ContextPackage"]
