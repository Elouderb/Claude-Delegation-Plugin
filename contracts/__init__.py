"""Agent OS contracts — Phase 0 stable interfaces (proposal §16 Phase 0 / §22).

This top-level package is the *dependency of everything and dependent on nothing*
in Agent OS: it defines the typed contract models (pydantic v2) that events,
tasks, memory, capabilities, agents, and context packages are exchanged as, plus
stable machine/repository identity and prefixed-ULID identifiers.  It imports
only the standard library and pydantic and must never import from ``mcp/``
(ruling 1).

The committed JSON Schemas under ``contracts/schemas/*.json`` are generated from
these models by ``contracts.generate_schemas``; a drift test proves regeneration
is byte-identical.  ``contracts.examples`` constructs one valid instance of every
model (also runnable as ``python -m contracts.examples``).

:data:`ALL_MODELS` is the authoritative registry of concrete contract models,
derived by introspection so a newly-added model is automatically included — and
therefore forces both schema generation and example coverage to account for it.
"""

from __future__ import annotations

from .base import (
    AgentOSModel,
    Dotted,
    NAMESPACED_NAME,
    strict_validate,
    utc_now,
)
from .identifiers import (
    AGENT,
    EVENT,
    GRANT,
    KNOWN_PREFIXES,
    MACHINE,
    MEMORY,
    PROJECT,
    PROPOSAL,
    REPOSITORY,
    SESSION,
    TASK,
    generate_ulid,
    is_valid_id,
    is_valid_ulid,
    new_id,
    split_id,
)
from .events import EventEnvelope, EventType
from .capabilities import Capability, CapabilityGrant
from .registry import AgentRegistration, AgentStatus
from .tasks import TaskRecord, TaskSyncStatus
from .memory import (
    MemoryApprovalState,
    MemoryEdge,
    MemoryEdgeType,
    MemoryNode,
)
from .artifacts import (
    Approval,
    Blocker,
    Claim,
    DecisionRequest,
    ExecutionReport,
    Finding,
    MemoryObservation,
    MemoryProposal,
    PatchReference,
    Plan,
    Question,
    StatusReport,
    Task,
    TestReport,
)
from .context import ContextPackage
from .identity import (
    MachineIdentity,
    RepositoryIdentity,
    ensure_machine_identity,
    ensure_repository_identity,
    machine_home,
    machine_identity_path,
    new_project_id,
    repository_identity_path,
)

# Authoritative registry of concrete contract models: every AgentOSModel subclass
# imported into this namespace, except the abstract base itself.  Derived by
# introspection (not a hand-maintained list) so adding a model automatically
# enrolls it in schema generation and example-coverage checks.
ALL_MODELS = tuple(
    sorted(
        (
            obj
            for obj in list(globals().values())
            if isinstance(obj, type)
            and issubclass(obj, AgentOSModel)
            and obj is not AgentOSModel
            and obj.__module__.startswith("contracts.")
        ),
        key=lambda cls: cls.__name__,
    )
)

# Mapping name -> model class, for schema/example lookups.
MODELS_BY_NAME = {cls.__name__: cls for cls in ALL_MODELS}

__all__ = [
    # base + registry
    "AgentOSModel",
    "utc_now",
    "strict_validate",
    "Dotted",
    "NAMESPACED_NAME",
    "ALL_MODELS",
    "MODELS_BY_NAME",
    # identifiers
    "generate_ulid",
    "is_valid_ulid",
    "is_valid_id",
    "new_id",
    "split_id",
    "KNOWN_PREFIXES",
    "EVENT",
    "TASK",
    "MACHINE",
    "REPOSITORY",
    "PROJECT",
    "AGENT",
    "SESSION",
    "MEMORY",
    "PROPOSAL",
    "GRANT",
    # events
    "EventEnvelope",
    "EventType",
    # capabilities
    "Capability",
    "CapabilityGrant",
    # registry
    "AgentRegistration",
    "AgentStatus",
    # tasks
    "TaskRecord",
    "TaskSyncStatus",
    # memory
    "MemoryNode",
    "MemoryEdge",
    "MemoryEdgeType",
    "MemoryApprovalState",
    # artifacts
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
    # context
    "ContextPackage",
    # identity
    "MachineIdentity",
    "RepositoryIdentity",
    "ensure_machine_identity",
    "ensure_repository_identity",
    "machine_home",
    "machine_identity_path",
    "repository_identity_path",
    "new_project_id",
]
