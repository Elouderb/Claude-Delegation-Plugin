"""Capability registry and grant model (proposal §12).

Agent OS uses *explicit capabilities* rather than broad trust categories.  The
:class:`Capability` enum is the registry from §12.1; a :class:`CapabilityGrant`
records that a principal (an agent or a machine) has been granted a capability,
optionally scoped and time-bounded.

Policy note (documented, not enforced in Phase 0): the model is **default-deny**
— a principal has *only* the capabilities explicitly granted to it.  Phase 0
ships the contract shape; enforcement arrives with the server/API in Phase 1.
See ``CONTRACTS.md``.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import AwareDatetime, Field

from .base import AgentOSModel, Dotted


class Capability(str, Enum):
    """Example capability set (proposal §12.1) — the **producer-side registry**.

    §12.1 is explicitly an *example* set, and later machines add their own
    capabilities (e.g. ``telegram.send``, ``schedule.create``).  So this enum is
    the documented, checked registry of the capabilities the plugin ships with —
    NOT the type of a capability field.  ``CapabilityGrant.capability`` and
    ``AgentRegistration.capabilities`` are open, dot-namespaced ``domain.verb``
    strings: they accept a registry value today and an unknown capability
    tomorrow without a contract change.  This is safe because the enforcement
    model is **default-deny** (``CONTRACTS.md`` Decision 4) — an unknown
    capability grants nothing until policy recognises it — whereas a closed
    ``Enum`` field would instead break parsing of any registration or grant that
    mentions a capability the local build has not heard of.  Grouped by domain:
    repository access, shell, cards, project/global memory, database, email, and
    coordination/deploy actions.  Additive within a schema major version.
    """

    REPO_READ = "repo.read"
    REPO_WRITE = "repo.write"
    REPO_COMMIT = "repo.commit"

    SHELL_EXECUTE = "shell.execute"
    SHELL_NETWORK = "shell.network"

    CARDS_READ = "cards.read"
    CARDS_WRITE = "cards.write"

    PROJECT_MEMORY_READ = "project_memory.read"
    PROJECT_MEMORY_PROPOSE = "project_memory.propose"
    PROJECT_MEMORY_COMMIT = "project_memory.commit"

    GLOBAL_MEMORY_READ = "global_memory.read"
    GLOBAL_MEMORY_PROPOSE = "global_memory.propose"
    GLOBAL_MEMORY_COMMIT = "global_memory.commit"

    DATABASE_SCHEMA_READ = "database_schema.read"
    DATABASE_ROWS_READ = "database_rows.read"
    DATABASE_WRITE = "database.write"

    EMAIL_READ = "email.read"
    EMAIL_DRAFT = "email.draft"
    EMAIL_SEND = "email.send"

    TASK_ASSIGN = "task.assign"
    AGENT_INTERRUPT = "agent.interrupt"
    DEPLOY_EXECUTE = "deploy.execute"


class CapabilityGrant(AgentOSModel):
    """A capability granted to a principal (proposal §12).

    The principal is identified by ``principal_type`` (``agent`` or ``machine``)
    and ``principal_id``.  ``scope`` optionally narrows the grant (e.g. a single
    repository id); ``expires_at`` bounds it in time (``None`` = no expiry).
    """

    grant_id: str = Field(
        description="Prefixed ULID for this durable grant record (grant_...)."
    )
    capability: Dotted = Field(
        description=(
            "Open dot-namespaced capability (e.g. 'repo.write'). A Capability "
            "registry value today; any well-formed domain.verb string tomorrow "
            "(default-deny keeps an unrecognised capability harmless)."
        )
    )
    principal_type: Literal["agent", "machine"]
    principal_id: str = Field(description="Id of the granted agent or machine.")
    scope: Optional[str] = Field(
        default=None,
        description="Optional scope narrowing the grant (e.g. a repository id).",
    )
    granted_by: str = Field(
        description="Id of the principal or policy that issued the grant."
    )
    granted_at: AwareDatetime
    expires_at: Optional[AwareDatetime] = Field(
        default=None, description="Grant expiry (UTC); None means no expiry."
    )


__all__ = ["Capability", "CapabilityGrant"]
