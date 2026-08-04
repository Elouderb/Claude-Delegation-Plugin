"""Structured agent-communication artifacts (proposal §11).

Agents exchange *structured artifacts*, not raw transcripts (proposal §11 /
epic governing decisions).  This module defines the §11.1 core artifact set.

Every artifact carries an ``artifact_type`` discriminator and an id.  Two id
conventions coexist and are documented in ``CONTRACTS.md``:

  * durable entities use a *registered prefixed ULID* (ruling 2) — here that is
    :class:`MemoryProposal`, which is a durable proposal and uses a ``prop_`` id;
  * transient inter-agent artifacts carry a free-form ``artifact_id`` string
    (the §11.2 example uses ``finding_...``), because they are messages rather
    than registered durable objects.

Most §11.1 artifacts share the same ``artifact_id`` + optional ``task_id`` head,
and most also carry an authoring agent, so they derive from two thin
intermediates — :class:`ArtifactBase` and :class:`AuthoredArtifact` — instead of
repeating the trio.  These intermediates are internal (not exported / not
registered as concrete contract models); each concrete artifact still declares
its own ``artifact_type`` Literal discriminator.

:class:`Claim` (statement + confidence + evidence, per §11.2) is the shared unit
of evidence-bearing assertion used by findings and observations.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import AwareDatetime, Field

from .base import AgentOSModel
from .registry import AgentStatus


class Claim(AgentOSModel):
    """An evidence-bearing assertion (proposal §11.2).

    ``confidence`` is in [0, 1]; ``evidence`` holds reference strings such as
    ``dbgraph://CustomerOrder/CustomerId`` or ``event:...`` locating the support.
    """

    statement: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: List[str] = Field(default_factory=list)


class ArtifactBase(AgentOSModel):
    """Shared head for §11.1 artifacts: a free-form id + optional task link.

    Internal intermediate — not exported and not a registered contract model.
    """

    artifact_id: str
    task_id: Optional[str] = Field(
        default=None, description="Linked canonical task id (task_...), if any."
    )


class AuthoredArtifact(ArtifactBase):
    """An :class:`ArtifactBase` with a required authoring agent.

    Internal intermediate — not exported and not a registered contract model.
    """

    author_agent: str


class Task(ArtifactBase):
    """A structured task description passed between agents (proposal §11.1)."""

    artifact_type: Literal["task"] = "task"
    title: str
    description: Optional[str] = None
    acceptance_criteria: List[str] = Field(default_factory=list)
    priority: Optional[str] = None


class Plan(AuthoredArtifact):
    """An ordered plan of steps for a task (proposal §11.1)."""

    artifact_type: Literal["plan"] = "plan"
    summary: str
    steps: List[str] = Field(default_factory=list)


class Finding(AuthoredArtifact):
    """A finding: evidence-bearing claims + recommended actions (proposal §11.2)."""

    artifact_type: Literal["finding"] = "finding"
    claims: List[Claim] = Field(default_factory=list)
    recommended_actions: List[str] = Field(default_factory=list)


class Question(AuthoredArtifact):
    """A question raised by an agent (proposal §11.1)."""

    artifact_type: Literal["question"] = "question"
    question: str
    context: Optional[str] = None


class Blocker(AuthoredArtifact):
    """A blocker preventing progress (proposal §11.1)."""

    artifact_type: Literal["blocker"] = "blocker"
    summary: str
    blocked_reason: str
    needs: List[str] = Field(
        default_factory=list, description="What would unblock this."
    )


class DecisionRequest(AuthoredArtifact):
    """A request for a human/policy decision between options (proposal §11.1)."""

    artifact_type: Literal["decision_request"] = "decision_request"
    question: str
    options: List[str] = Field(default_factory=list)
    recommendation: Optional[str] = None


class Approval(AgentOSModel):
    """An approval/rejection of a requested action (proposal §11.1 / §12.3).

    Not an :class:`ArtifactBase`: an approval is authored by an ``approver``
    (a user/policy, not an agent) and references a ``decision_request_id`` rather
    than a task, so it carries neither the ``author_agent`` nor the ``task_id``
    head.
    """

    artifact_type: Literal["approval"] = "approval"
    artifact_id: str
    decision: Literal["approved", "rejected"]
    requested_action: str
    approver: str = Field(description="Approving user or policy id.")
    reason: Optional[str] = None
    decision_request_id: Optional[str] = None
    decided_at: AwareDatetime


class PatchReference(ArtifactBase):
    """A reference to a code change (proposal §11.1).

    A *reference*, not the diff itself: commit/branch identifiers and a summary,
    so reports can point at a change without forwarding its full contents.

    ``author_agent`` is intentionally **optional** here (unlike the authored
    artifacts): a patch reference is often produced mechanically from git and its
    author is typically implied by the enclosing :class:`ExecutionReport`, so the
    field is not required on the reference itself.
    """

    artifact_type: Literal["patch_reference"] = "patch_reference"
    author_agent: Optional[str] = None
    repository_id: Optional[str] = None
    commit: Optional[str] = None
    branch: Optional[str] = None
    diff_summary: Optional[str] = None
    files_changed: List[str] = Field(default_factory=list)


class TestReport(ArtifactBase):
    """A test-run result summary (proposal §11.1).

    ``author_agent`` is intentionally **optional** here (unlike the authored
    artifacts): a test report is usually emitted by a test runner rather than a
    distinct authoring agent, and its author is implied by the enclosing
    :class:`ExecutionReport` when one exists.
    """

    artifact_type: Literal["test_report"] = "test_report"
    author_agent: Optional[str] = None
    summary: str
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    details: Optional[str] = None


class ExecutionReport(AuthoredArtifact):
    """An agent's report on executing a task (proposal §11.1)."""

    artifact_type: Literal["execution_report"] = "execution_report"
    status: Literal["success", "failure", "partial"]
    summary: str
    patch_reference_ids: List[str] = Field(default_factory=list)
    test_report_id: Optional[str] = None
    details: Optional[str] = None


class MemoryObservation(AuthoredArtifact):
    """A raw observation an agent offers toward memory (proposal §11.1 / §18.4).

    Execution agents *observe*; they do not commit memory directly.  An
    observation is the pre-curation input to the memory pipeline (§9.6).
    """

    artifact_type: Literal["memory_observation"] = "memory_observation"
    observation: str
    evidence: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class MemoryProposal(AgentOSModel):
    """A proposal to record/promote a memory (proposal §11.1 / §5.3 / §9.7).

    Durable proposal object: carries a registered ``prop_`` id (so it is not an
    :class:`ArtifactBase`, which uses a free-form ``artifact_id``).
    ``target_scope`` says whether the proposal is for project or global memory;
    ``claim`` + ``evidence`` + ``confidence`` justify it; curator/consolidation
    logic (later phases) decides acceptance.
    """

    artifact_type: Literal["memory_proposal"] = "memory_proposal"
    proposal_id: str = Field(description="Prefixed ULID for the proposal (prop_...).")
    author_agent: str
    task_id: Optional[str] = None
    target_scope: Literal["project", "global"] = "project"
    node_type: str = Field(description="Proposed memory node type.")
    claim: str
    evidence: List[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: Optional[str] = None
    created_at: AwareDatetime


class StatusReport(AuthoredArtifact):
    """A periodic agent status/progress report (proposal §11.1)."""

    artifact_type: Literal["status_report"] = "status_report"
    session_id: Optional[str] = None
    status: AgentStatus
    summary: str
    progress: Optional[float] = Field(
        default=None, ge=0.0, le=1.0, description="Fractional progress in [0, 1]."
    )
    blockers: List[str] = Field(default_factory=list)
    reported_at: AwareDatetime


__all__ = [
    "Claim",
    "Task",
    "Plan",
    "Finding",
    "Question",
    "Blocker",
    "DecisionRequest",
    "Approval",
    "PatchReference",
    "TestReport",
    "ExecutionReport",
    "MemoryObservation",
    "MemoryProposal",
    "StatusReport",
]
