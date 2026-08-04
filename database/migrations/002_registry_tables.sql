-- Migration 002: registry.* — machine, repository, and agent-session identity
-- (proposal §17 first slice). Column shapes round-trip contracts.identity and
-- contracts.registry (see contracts/identity.py: MachineIdentity,
-- RepositoryIdentity; contracts/registry.py: AgentRegistration).
--
-- ID sizing: every prefixed-ULID id column here is NVARCHAR(64) uniformly
-- (documented decision — see database/migrate.py module docstring and
-- database/README.md) rather than a per-prefix exact width, so a future
-- longer prefix never forces a migration.

CREATE TABLE registry.machines (
    machine_id      NVARCHAR(64)    NOT NULL,
    name            NVARCHAR(200)   NOT NULL,
    os              NVARCHAR(100)   NOT NULL,
    -- Mirrors contracts.identity.MachineIdentity.created_at (when the local
    -- ~/.agent-os/identity.json was first created on that machine).
    created_at      DATETIMEOFFSET  NOT NULL,
    -- When this machine was first registered with the CENTRAL store — may be
    -- later than created_at if the machine ran local-only for a while first.
    registered_at   DATETIMEOFFSET  NOT NULL
        CONSTRAINT DF_registry_machines_registered_at DEFAULT (SYSDATETIMEOFFSET()),
    last_seen_at    DATETIMEOFFSET  NULL,
    CONSTRAINT PK_registry_machines PRIMARY KEY (machine_id)
);

CREATE TABLE registry.repositories (
    repository_id     NVARCHAR(64)    NOT NULL,
    machine_id         NVARCHAR(64)    NOT NULL,
    -- Normalized origin-remote fingerprint (contracts.identity.canonical_remote_url);
    -- NULL when no remote is known (e.g. a purely local checkout).
    canonical_remote     NVARCHAR(500)   NULL,
    -- Owning project id; NULL until a project is assigned (mirrors
    -- contracts.identity.RepositoryIdentity.project_id).
    project_id             NVARCHAR(64)    NULL,
    repo_root                NVARCHAR(1000)  NOT NULL,
    -- Natural-key hash of repo_root. repo_root is NVARCHAR(1000) = 2000 bytes,
    -- which exceeds SQL Server's 1700-byte index key limit, so it cannot be
    -- indexed directly; this deterministic, PERSISTED SHA2_256 (BINARY(32))
    -- computed column can. It backs the (machine_id, repo_root_hash) uniqueness
    -- below so a machine cannot accumulate DUPLICATE rows for the same checkout
    -- path when .agent-os is deleted (e.g. `git clean -fdx`) and the repo id is
    -- re-minted — otherwise the M3 board would double-count that checkout.
    repo_root_hash           AS CONVERT(BINARY(32), HASHBYTES('SHA2_256', repo_root)) PERSISTED,
    created_at                 DATETIMEOFFSET  NOT NULL,
    registered_at                 DATETIMEOFFSET  NOT NULL
        CONSTRAINT DF_registry_repositories_registered_at DEFAULT (SYSDATETIMEOFFSET()),
    last_seen_at                     DATETIMEOFFSET  NULL,
    CONSTRAINT PK_registry_repositories PRIMARY KEY (repository_id),
    CONSTRAINT FK_registry_repositories_machine FOREIGN KEY (machine_id)
        REFERENCES registry.machines (machine_id)
);

CREATE INDEX IX_registry_repositories_machine_id
    ON registry.repositories (machine_id);

-- Natural key: one repository row per (machine, checkout path). machine_id
-- (128 bytes) + repo_root_hash (32 bytes) = 160 bytes, well under the 1700-byte
-- index key limit. A re-registration of the same checkout must UPSERT this row,
-- not insert a duplicate (upsert logic is the API layer's job, later phase; this
-- constraint makes a duplicate impossible at the schema level).
CREATE UNIQUE INDEX UQ_registry_repositories_natural
    ON registry.repositories (machine_id, repo_root_hash);

CREATE TABLE registry.agent_sessions (
    session_id           NVARCHAR(64)    NOT NULL,
    -- Not unique in this table by design: an agent may restart across
    -- multiple sessions over time, so agent_id is a plain column, not a key.
    agent_id               NVARCHAR(64)    NOT NULL,
    machine_id               NVARCHAR(64)    NOT NULL,
    repository_id              NVARCHAR(64)    NULL,
    agent_type                   NVARCHAR(100)   NOT NULL,
    -- Spawning agent's session/agent id, if this is a subagent; no FK (the
    -- parent may belong to a different, possibly already-finished, session).
    parent_agent_id                NVARCHAR(64)    NULL,
    -- Mirrors contracts.registry.AgentStatus (starting/running/waiting/failed/finished).
    status                            NVARCHAR(20)    NOT NULL
        CONSTRAINT CK_registry_agent_sessions_status
        CHECK (status IN ('starting', 'running', 'waiting', 'failed', 'finished')),
    -- JSON array of open dot-namespaced capability strings
    -- (contracts.registry.AgentRegistration.capabilities). NVARCHAR(MAX) for
    -- driver simplicity this phase — see database/migrate.py module docstring.
    capabilities                        NVARCHAR(MAX)   NOT NULL
        CONSTRAINT DF_registry_agent_sessions_capabilities DEFAULT (N'[]'),
    current_task_id                       NVARCHAR(64)    NULL,
    -- Maps to contracts.registry.AgentRegistration.runtime (e.g. "claude-opus").
    model_runtime                            NVARCHAR(200)   NULL,
    started_at                                 DATETIMEOFFSET  NOT NULL,
    -- Maps to contracts.registry.AgentRegistration.heartbeat_at.
    last_heartbeat_at                             DATETIMEOFFSET  NULL,
    -- Maps to contracts.registry.AgentRegistration.expected_expiration_at.
    expires_at                                       DATETIMEOFFSET  NULL,
    CONSTRAINT PK_registry_agent_sessions PRIMARY KEY (session_id),
    CONSTRAINT FK_registry_agent_sessions_machine FOREIGN KEY (machine_id)
        REFERENCES registry.machines (machine_id),
    CONSTRAINT FK_registry_agent_sessions_repository FOREIGN KEY (repository_id)
        REFERENCES registry.repositories (repository_id)
);

CREATE INDEX IX_registry_agent_sessions_machine_id
    ON registry.agent_sessions (machine_id);
CREATE INDEX IX_registry_agent_sessions_repository_id
    ON registry.agent_sessions (repository_id);
CREATE INDEX IX_registry_agent_sessions_status
    ON registry.agent_sessions (status);
