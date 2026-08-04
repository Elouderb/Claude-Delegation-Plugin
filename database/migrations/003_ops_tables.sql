-- Migration 003: ops.* — event log + sync/ingest bookkeeping (proposal §17
-- first slice / §20 idempotent delivery). ops.events column shapes round-trip
-- contracts.events.EventEnvelope (see contracts/events.py).
--
-- Deliberately NO foreign keys from ops.events / ops.ingest_errors /
-- ops.sync_cursors back to registry.* : an event/error/cursor row must be
-- acceptable for a machine/repository/session that raced ahead of (or
-- predates, or outlives) its registry row, and must never be blocked or
-- cascade-deleted by registry churn. Referential integrity for these ids is
-- an API-layer concern for a later phase, not a schema-level one here.

CREATE TABLE ops.events (
    -- PRIMARY KEY on event_id gives duplicate-delivery idempotence per §20:
    -- a redelivered event with the same id fails the PK and is dropped/
    -- dead-lettered by the caller rather than double-recorded.
    event_id        NVARCHAR(64)    NOT NULL,
    event_type       NVARCHAR(200)   NOT NULL,
    -- When the event occurred at its origin (contracts.events.EventEnvelope.occurred_at).
    occurred_at        DATETIMEOFFSET  NOT NULL,
    -- When this row was written centrally — distinct from occurred_at because
    -- delivery may lag origin (offline agent, outbox retry, etc.).
    received_at           DATETIMEOFFSET  NOT NULL
        CONSTRAINT DF_ops_events_received_at DEFAULT (SYSDATETIMEOFFSET()),
    machine_id               NVARCHAR(64)    NOT NULL,
    agent_id                   NVARCHAR(64)    NULL,
    session_id                   NVARCHAR(64)    NULL,
    project_id                     NVARCHAR(64)    NULL,
    repository_id                    NVARCHAR(64)    NULL,
    task_id                            NVARCHAR(64)    NULL,
    -- Event-type-specific body; opaque to the schema. NVARCHAR(MAX) for
    -- driver simplicity this phase — see database/migrate.py module docstring
    -- (SQL Server 2025's native json type is a documented revisit).
    payload                               NVARCHAR(MAX)   NOT NULL
        CONSTRAINT DF_ops_events_payload DEFAULT (N'{}'),
    schema_version                          INT             NOT NULL
        CONSTRAINT DF_ops_events_schema_version DEFAULT (1),
    CONSTRAINT PK_ops_events PRIMARY KEY (event_id)
);

CREATE INDEX IX_ops_events_type_occurred
    ON ops.events (event_type, occurred_at);
CREATE INDEX IX_ops_events_machine_occurred
    ON ops.events (machine_id, occurred_at);
CREATE INDEX IX_ops_events_repository_occurred
    ON ops.events (repository_id, occurred_at);

-- Per-client (consumer) replay position in the event stream. "client" is
-- deliberately broader than a registered machine (e.g. a stand-in M3 client,
-- or a future external MCP gateway consumer), so client_id is unconstrained.
CREATE TABLE ops.sync_cursors (
    client_id       NVARCHAR(64)    NOT NULL,
    -- NULL: this client has no recorded replay position yet.
    last_event_id    NVARCHAR(64)    NULL,
    updated_at         DATETIMEOFFSET  NOT NULL
        CONSTRAINT DF_ops_sync_cursors_updated_at DEFAULT (SYSDATETIMEOFFSET()),
    CONSTRAINT PK_ops_sync_cursors PRIMARY KEY (client_id)
);

-- Dead-letter style landing zone for inbound payloads that failed to parse/
-- validate as a known contract — kept so a malformed delivery is diagnosable
-- instead of silently dropped. source_machine_id is unconstrained/nullable:
-- a malformed payload may not even carry a recognizable machine id.
CREATE TABLE ops.ingest_errors (
    id                 INT             IDENTITY(1,1) NOT NULL,
    received_at           DATETIMEOFFSET  NOT NULL
        CONSTRAINT DF_ops_ingest_errors_received_at DEFAULT (SYSDATETIMEOFFSET()),
    source_machine_id       NVARCHAR(64)    NULL,
    reason                    NVARCHAR(1000)  NOT NULL,
    raw                         NVARCHAR(MAX)   NULL,
    CONSTRAINT PK_ops_ingest_errors PRIMARY KEY (id)
);
