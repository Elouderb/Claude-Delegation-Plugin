"""One valid example instance of every contract model (ruling 11 / §16 success).

This module is the Phase-0 realization of the §16 success criterion "existing
plugin can emit example payloads": it builds a canonical, *valid* instance of
every model in :data:`contracts.ALL_MODELS`, keyed by class name in
:data:`EXAMPLES`.  The examples are used by the test-suite (round-trip and
coverage checks) and can be run directly::

    python -m contracts.examples

which prints each model's validated JSON.  No hook wiring exists yet (that is
Phase 1); this is purely a contract-emission demonstrator.

Each example is expressed as a small dict of constructor kwargs in
:data:`_EXAMPLE_KWARGS`, and instances are built by looking the class up in
``contracts.MODELS_BY_NAME`` — so the example set is data, not 300 lines of
imperative construction.  Coverage is enforced, not hoped for:
:func:`check_coverage` derives its required set from ``ALL_MODELS`` (itself
introspected), so a newly-added model with no entry in ``_EXAMPLE_KWARGS`` fails
:func:`check_coverage` — and the test that calls it — loudly.

The :class:`~contracts.artifacts.Finding` example reproduces proposal §11.2
verbatim; the :class:`~contracts.memory.MemoryNode` example reproduces §4.3.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

from . import MODELS_BY_NAME
from .base import AgentOSModel
from .artifacts import Claim
from .capabilities import Capability
from .events import EventType
from .identifiers import (
    AGENT,
    EVENT,
    GRANT,
    MACHINE,
    MEMORY,
    PROJECT,
    PROPOSAL,
    REPOSITORY,
    SESSION,
    TASK,
)
from .memory import MemoryApprovalState, MemoryEdgeType
from .registry import AgentStatus
from .tasks import TaskSyncStatus

# A fixed, valid Crockford-base32 ULID body + timestamp so example output is
# deterministic (the drift/round-trip tests compare bytes).
_ULID = "01J8Z3M4Q5R6S7T8V9W0X1Y2Z3"
_TS = datetime(2026, 8, 3, 20, 0, 0, tzinfo=timezone.utc)


def _id(prefix: str) -> str:
    """A deterministic example prefixed id, e.g. ``evt_01J8Z...``."""
    return f"{prefix}_{_ULID}"


# One example's constructor kwargs per model (name -> non-default fields).  Open
# type-registry fields (event_type / capability) carry registry ``.value``
# strings, demonstrating the "producer uses the registry, field type is an open
# string" posture.
_EXAMPLE_KWARGS: Dict[str, Dict[str, Any]] = {
    # --- identifiers / identity ---
    "MachineIdentity": dict(
        machine_id=_id(MACHINE), name="machine-1", os="Linux", created_at=_TS
    ),
    "RepositoryIdentity": dict(
        repository_id=_id(REPOSITORY),
        project_id=_id(PROJECT),
        canonical_remote="github.com/acme/widgets",
        machine_id=_id(MACHINE),
        created_at=_TS,
    ),
    # --- events ---
    "EventEnvelope": dict(
        event_id=_id(EVENT),
        event_type=EventType.TASK_COMPLETED.value,
        occurred_at=_TS,
        machine_id=_id(MACHINE),
        agent_id=_id(AGENT),
        session_id=_id(SESSION),
        project_id=_id(PROJECT),
        repository_id=_id(REPOSITORY),
        task_id=_id(TASK),
        payload={"result": "ok"},
    ),
    # --- capabilities / registry ---
    "CapabilityGrant": dict(
        grant_id=_id(GRANT),
        capability=Capability.REPO_WRITE.value,
        principal_type="agent",
        principal_id=_id(AGENT),
        scope=_id(REPOSITORY),
        granted_by="user:ethan",
        granted_at=_TS,
        expires_at=None,
    ),
    "AgentRegistration": dict(
        agent_id=_id(AGENT),
        session_id=_id(SESSION),
        agent_type="implementer",
        machine_id=_id(MACHINE),
        repository_id=_id(REPOSITORY),
        parent_agent_id=None,
        capabilities=[Capability.REPO_READ.value, Capability.CARDS_WRITE.value],
        current_task_id=_id(TASK),
        status=AgentStatus.RUNNING,
        runtime="claude-opus",
        started_at=_TS,
        heartbeat_at=_TS,
        expected_expiration_at=None,
    ),
    # --- tasks (sync) ---
    "TaskRecord": dict(
        global_id=_id(TASK),
        local_card_id="ab12cd34",
        project_id=_id(PROJECT),
        repository_id=_id(REPOSITORY),
        title="Implement Phase 0 contracts",
        description="Define typed contract models + committed JSON Schemas.",
        priority="high",
        status=TaskSyncStatus.IN_PROGRESS,
        status_reason="Actively implementing the contracts package.",
        version=1,
        updated_at=_TS,
        originating_client=_id(MACHINE),
        last_status_event_id=_id(EVENT),
    ),
    # --- memory ---
    "MemoryNode": dict(
        node_id=_id(MEMORY),
        node_type="architectural_pattern",
        canonical_name="Dedicated memory curator",
        summary=(
            "Durable memory writes should be mediated by a curator rather "
            "than written directly by every execution agent."
        ),
        scope="global",
        project_id=_id(PROJECT),
        repository_id=_id(REPOSITORY),
        confidence=0.91,
        stability="medium",
        approval_state=MemoryApprovalState.ACCEPTED,
        evidence=["decision:5.5", f"event:{_id(EVENT)}"],
        created_by="agent:machine3-memory-worker",
        created_at=_TS,
        last_verified_at=_TS,
    ),
    "MemoryEdge": dict(
        edge_id=_id(MEMORY),
        source_node_id=_id(MEMORY),
        target_node_id=_id(MEMORY),
        edge_type=MemoryEdgeType.SUPPORTED_BY,
        weight=0.8,
        confidence=0.9,
        metadata={"note": "example"},
    ),
    # --- artifacts (§11.1) ---
    "Claim": dict(
        statement="Example claim.", confidence=0.9, evidence=["event:example"]
    ),
    "Task": dict(
        artifact_id=f"task_artifact_{_ULID}",
        title="Implement identity contracts",
        description="Define machine/repo identity models.",
        acceptance_criteria=["Idempotent creation", "Atomic write"],
        priority="high",
        task_id=_id(TASK),
    ),
    "Plan": dict(
        artifact_id=f"plan_{_ULID}",
        author_agent="complex-implementer",
        task_id=_id(TASK),
        summary="Two-step plan.",
        steps=["Write models", "Write tests"],
    ),
    # Finding — proposal §11.2 verbatim.
    "Finding": dict(
        artifact_id=f"finding_{_ULID}",
        author_agent="database-engineer",
        task_id="TASK-184",
        claims=[
            Claim(
                statement="CustomerOrder.CustomerId has no foreign-key constraint.",
                confidence=0.99,
                evidence=["dbgraph://CustomerOrder/CustomerId"],
            )
        ],
        recommended_actions=[
            "Determine whether the missing constraint is intentional before "
            "modifying the schema."
        ],
    ),
    "Question": dict(
        artifact_id=f"question_{_ULID}",
        author_agent="implementer",
        task_id=_id(TASK),
        question="Should identity ids be UUIDv4 or ULID?",
        context="Ruling 2 selects prefixed ULIDs.",
    ),
    "Blocker": dict(
        artifact_id=f"blocker_{_ULID}",
        author_agent="implementer",
        task_id=_id(TASK),
        summary="Missing dependency.",
        blocked_reason="pydantic not installed in venv.",
        needs=["pip install pydantic"],
    ),
    "DecisionRequest": dict(
        artifact_id=f"decision_request_{_ULID}",
        author_agent="research-planner",
        task_id=_id(TASK),
        question="Frozen or mutable contract models?",
        options=["frozen", "mutable"],
        recommendation="mutable",
    ),
    "Approval": dict(
        artifact_id=f"approval_{_ULID}",
        decision="approved",
        requested_action="deploy.execute",
        approver="user:ethan",
        reason="Change reviewed.",
        decision_request_id=f"decision_request_{_ULID}",
        decided_at=_TS,
    ),
    "PatchReference": dict(
        artifact_id=f"patch_{_ULID}",
        author_agent="implementer",
        task_id=_id(TASK),
        repository_id=_id(REPOSITORY),
        commit="0781258",
        branch="release/0.2.7",
        diff_summary="Add contracts package.",
        files_changed=["contracts/__init__.py"],
    ),
    "TestReport": dict(
        artifact_id=f"test_{_ULID}",
        author_agent="test-engineer",
        task_id=_id(TASK),
        summary="All green.",
        passed=336,
        failed=0,
        skipped=1,
        details="pytest mcp/tests",
    ),
    "ExecutionReport": dict(
        artifact_id=f"exec_{_ULID}",
        author_agent="complex-implementer",
        task_id=_id(TASK),
        status="success",
        summary="Contracts implemented.",
        patch_reference_ids=[f"patch_{_ULID}"],
        test_report_id=f"test_{_ULID}",
        details=None,
    ),
    "MemoryObservation": dict(
        artifact_id=f"observation_{_ULID}",
        author_agent="implementer",
        task_id=_id(TASK),
        observation="Outbox pattern prevents central-outage corruption.",
        evidence=["decision:5.5"],
        confidence=0.8,
    ),
    "MemoryProposal": dict(
        proposal_id=_id(PROPOSAL),
        author_agent="agent:machine3-memory-worker",
        task_id=_id(TASK),
        target_scope="global",
        node_type="procedure",
        claim="Use an outbox before central synchronization.",
        evidence=["event:sync", "decision:5.5"],
        confidence=0.94,
        reason="Repeatedly useful for resilient distributed agent state.",
        created_at=_TS,
    ),
    "StatusReport": dict(
        artifact_id=f"status_{_ULID}",
        author_agent="implementer",
        session_id=_id(SESSION),
        task_id=_id(TASK),
        status=AgentStatus.RUNNING,
        summary="Implementing Phase 0 contracts.",
        progress=0.6,
        blockers=[],
        reported_at=_TS,
    ),
    # --- context ---
    "ContextPackage": dict(
        task={"global_id": _id(TASK), "title": "Phase 0 contracts"},
        repo={"repository_id": _id(REPOSITORY)},
        relevant_files=["contracts/__init__.py"],
        code_graph_nodes=["code://contracts.EventEnvelope"],
        db_graph_nodes=["dbgraph://memory.nodes"],
        project_memories=[_id(MEMORY)],
        global_memories=[_id(MEMORY)],
        recent_events=[_id(EVENT)],
        constraints=["Preserve local-only mode."],
        citations=["src_example"],
        token_budget=30000,
    ),
}


def build_examples() -> Dict[str, AgentOSModel]:
    """Return a mapping of model class name -> one valid instance of it.

    Each instance is built by resolving the class in ``contracts.MODELS_BY_NAME``
    and applying its :data:`_EXAMPLE_KWARGS` entry, so the example set stays data
    rather than imperative construction.
    """
    return {
        name: MODELS_BY_NAME[name](**kwargs)
        for name, kwargs in _EXAMPLE_KWARGS.items()
    }


# Built once at import; the mapping is stable and deterministic.
EXAMPLES: Dict[str, AgentOSModel] = build_examples()


def check_coverage() -> None:
    """Raise if EXAMPLES does not cover exactly the concrete contract models.

    Introspects ``contracts.ALL_MODELS`` so a new model with no example (or an
    example whose class is not registered) fails loudly.
    """
    from . import ALL_MODELS

    registered = {cls.__name__ for cls in ALL_MODELS}
    have = set(EXAMPLES)
    missing = registered - have
    extra = have - registered
    if missing or extra:
        raise AssertionError(
            f"example coverage mismatch: missing={sorted(missing)} "
            f"extra={sorted(extra)}"
        )


def main() -> None:
    """Validate coverage and print each example as validated JSON."""
    check_coverage()
    for name in sorted(EXAMPLES):
        model = EXAMPLES[name]
        # Prove it round-trips through JSON before emitting.
        type(model).model_validate_json(model.model_dump_json())
        payload = json.loads(model.model_dump_json())
        print(f"# {name}")
        print(json.dumps(payload, indent=2, sort_keys=True))
        print()


if __name__ == "__main__":
    main()


__all__ = ["EXAMPLES", "build_examples", "check_coverage", "main"]
