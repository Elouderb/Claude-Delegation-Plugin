-- Migration 006: registry.tasks — the central task down-projection read model
-- (proposal §14 task model / §18.2 "central canonical identity + local durable
-- projections"). Phase 2a, card ffcaf548. Additive, backward-compatible.
--
-- This is the central materialization of a repository-local card: the API's
-- task materializer (services/api/store.py:_materialize_task, mirroring the
-- agent-session materializer) upserts one row here per accepted task.created /
-- task.updated / task.completed event that carries a `global_id`, and
-- GET /v1/tasks reads it so a SECOND machine can apply the task into its own
-- cards.sqlite. Column shapes round-trip contracts.tasks.TaskRecord (see
-- contracts/tasks.py) and carry the optimistic-concurrency metadata
-- (version / updated_at / originating_client / last_status_event_id) the NEXT
-- slice needs for true bidirectional reconciliation — so reconciliation is added
-- with no schema rebuild. THIS slice resolves conflicts by last-write-wins only.
--
-- NO foreign key from registry.tasks back to registry.repositories: exactly like
-- ops.events (see 003_ops_tables.sql), an event-DERIVED row must be materializable
-- for a repository that raced ahead of (or predates) its registry row, and must
-- never be blocked or cascade-deleted by registry churn. Referential integrity for
-- repository_id is an API-layer concern for a later phase, not a schema-level one.
--
-- ID sizing: every prefixed-ULID id column is NVARCHAR(64) uniformly, matching the
-- documented convention (database/migrate.py module docstring).

CREATE TABLE registry.tasks (
    -- Central canonical task identity (task_ ULID). Minted at card creation on the
    -- originating machine (mcp/card_tools.create_card).
    global_id           NVARCHAR(64)    NOT NULL,
    -- Originating repository's short local card id (contracts.tasks.local_card_id);
    -- informational — a projected card gets its OWN local card_id on each machine.
    local_card_id       NVARCHAR(64)    NULL,
    -- Owning repository / project (from the event envelope). No FK — see header.
    repository_id       NVARCHAR(64)    NULL,
    project_id          NVARCHAR(64)    NULL,
    -- Projectable content (proposal §14.1) — enough to render a local card.
    title               NVARCHAR(1000)  NULL,
    description         NVARCHAR(MAX)   NULL,
    priority            NVARCHAR(50)    NULL,
    -- Synchronizable status: a contracts.tasks.TaskSyncStatus value
    -- (created / in_progress / blocked / complete). Validated in app code (the
    -- local<->sync mapping in contracts/tasks.py), NOT by a DB CHECK, so the
    -- taxonomy can grow additively without a migration (mirrors the memory
    -- subsystem's app-validated source_type).
    status              NVARCHAR(50)    NOT NULL
        CONSTRAINT DF_registry_tasks_status DEFAULT (N'created'),
    status_reason       NVARCHAR(MAX)   NULL,
    -- Optimistic-concurrency metadata (proposal §14.3) — carried now so the next
    -- slice adds reconciliation with no rebuild.
    version             BIGINT          NOT NULL
        CONSTRAINT DF_registry_tasks_version DEFAULT (1),
    -- The machine id that produced this version (the event's originating machine).
    originating_client  NVARCHAR(64)    NULL,
    -- When the current state occurred at its origin (the event's occurred_at).
    updated_at          DATETIMEOFFSET  NOT NULL,
    -- The immutable status-history event (evt_ ULID) that produced this state.
    last_status_event_id NVARCHAR(64)   NULL,
    -- When this row was first materialized centrally.
    created_at          DATETIMEOFFSET  NOT NULL
        CONSTRAINT DF_registry_tasks_created_at DEFAULT (SYSDATETIMEOFFSET()),
    -- Database-monotonic change stamp: ROWVERSION advances on every INSERT AND
    -- every in-place UPDATE, giving a unique total order over this MUTABLE read
    -- model (an insert-only IDENTITY like ops.events.ingest_seq cannot, because an
    -- upsert re-writes an existing row). GET /v1/tasks?since= keyset-pages on
    -- CONVERT(BIGINT, change_seq); a down-projection poller resumes from the
    -- largest value it has applied. Auto-maintained — never written by app code.
    change_seq          ROWVERSION      NOT NULL,
    CONSTRAINT PK_registry_tasks PRIMARY KEY (global_id)
);

-- Repository-scoped down-reads (GET /v1/tasks?repository_id=).
CREATE INDEX IX_registry_tasks_repository
    ON registry.tasks (repository_id);

-- The keyset order for the down-read (WHERE change_seq > @since ORDER BY
-- change_seq ASC). ROWVERSION values are unique within the database.
CREATE UNIQUE INDEX IX_registry_tasks_change_seq
    ON registry.tasks (change_seq);
