"""Memory node and edge contracts (proposal §4.3, §4.5, §9.3-§9.4).

A memory *node* is a synthesized entity or concept — not a raw text chunk
(proposal §4.3).  A memory *edge* is a typed, weighted relationship between two
nodes (§4.5 / §9.4).  Phase 0 defines the durable shapes and the controlled
vocabularies (approval state §8.4, edge type §9.4); provenance, storage, and
promotion logic arrive in later phases.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import AwareDatetime, Field

from .base import AgentOSModel


class MemoryApprovalState(str, Enum):
    """Lifecycle state of a memory node/candidate (proposal §8.4)."""

    EXTRACTED = "extracted"
    CANDIDATE = "candidate"
    LINKED = "linked"
    VERIFIED = "verified"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    STALE = "stale"


class MemoryEdgeType(str, Enum):
    """Typed relationships between memory nodes (proposal §9.4)."""

    IMPLEMENTS = "IMPLEMENTS"
    DEPENDS_ON = "DEPENDS_ON"
    OWNS = "OWNS"
    DECIDED_BY = "DECIDED_BY"
    SUPERSEDES = "SUPERSEDES"
    CONTRADICTS = "CONTRADICTS"
    SUPPORTED_BY = "SUPPORTED_BY"
    DERIVED_FROM = "DERIVED_FROM"
    APPLIES_TO = "APPLIES_TO"
    BLOCKS = "BLOCKS"
    RESOLVES = "RESOLVES"
    RELATED_TO = "RELATED_TO"
    MENTIONS = "MENTIONS"
    USES = "USES"
    CONNECTS_TO = "CONNECTS_TO"
    PROMOTED_FROM = "PROMOTED_FROM"
    IMPORTED_FROM_GLOBAL = "IMPORTED_FROM_GLOBAL"


class MemoryNode(AgentOSModel):
    """A synthesized memory node (proposal §4.3).

    ``node_type`` and ``canonical_name`` name the concept; ``summary`` states it;
    ``scope`` places it (project vs global) and ``project_id`` / ``repository_id``
    say *which* project/repo it belongs to (a ``scope='project'`` node is
    meaningless without knowing the project); ``confidence`` and ``stability``
    rate it; ``approval_state`` tracks its lifecycle; ``evidence`` carries the
    provenance refs that support the node.

    ``evidence`` uses the same free-form reference-string convention as
    :class:`~contracts.artifacts.Claim` / :class:`~contracts.artifacts.MemoryProposal`
    — e.g. ``dbgraph://CustomerOrder/CustomerId``, ``event:evt_01J...``,
    ``chunk:...`` — locating the chunks or events the node was derived from.
    Provenance captured at write time is unrecoverable if lost, so the ref list
    exists from day one; the remaining §4.4 provenance facets
    (source version, contradiction state, access policy, …) are layered on
    additively in later phases.
    """

    node_id: str = Field(description="Prefixed ULID for the node (mem_...).")
    node_type: str = Field(description="Concept type, e.g. 'architectural_pattern'.")
    canonical_name: str
    summary: str
    scope: Literal["project", "global"] = "project"
    project_id: Optional[str] = Field(
        default=None,
        description="Owning project id (proj_...) for a project-scoped node.",
    )
    repository_id: Optional[str] = Field(
        default=None,
        description="Owning repository id (repo_...), when the node is repo-specific.",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    stability: Literal["low", "medium", "high"] = "medium"
    approval_state: MemoryApprovalState = MemoryApprovalState.CANDIDATE
    evidence: List[str] = Field(
        default_factory=list,
        description="Provenance reference strings (§4.4) supporting this node.",
    )
    created_by: str = Field(description="Authoring agent/principal id.")
    created_at: AwareDatetime
    last_verified_at: Optional[AwareDatetime] = None


class MemoryEdge(AgentOSModel):
    """A typed edge between two memory nodes (proposal §4.5 / §9.4)."""

    edge_id: str = Field(description="Prefixed ULID for the edge (mem_...).")
    source_node_id: str
    target_node_id: str
    edge_type: MemoryEdgeType
    weight: Optional[float] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    metadata: Dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "MemoryApprovalState",
    "MemoryEdgeType",
    "MemoryNode",
    "MemoryEdge",
]
