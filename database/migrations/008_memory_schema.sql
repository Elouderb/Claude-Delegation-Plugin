-- Migration 008: memory.documents + memory.chunks — the central RAG corpus
-- (epic 6fead93b, Slice 1b, card c8e686a9). Additive-only (new schema, new
-- tables); no existing object is touched.
--
-- RENUMBERING NOTE: this schema was sketched as a future "007" in the
-- Slice-0 design draft (scratchpad/slice0_memory_schema_DRAFT.sql, never
-- applied). 007 was claimed in the meantime by the machine-credentials
-- migration (007_machine_credentials.sql, card 8737706e) — see that file's
-- own renumbering note — so the central-memory schema takes 008.
--
-- Confirmed via live probes against 10.42.0.249 (SQL Server 2025, build
-- 17.0.4050.1, compat level 170, Linux) BEFORE this file was authored — see
-- card c8e686a9 for the full research trail:
--   * Native VECTOR(384) is a valid column type (float32 storage; exact
--     match with sqlite-vec's serialize_float32, no lossy conversion).
--     App-layer INSERT marshaling uses a single CAST(? AS VECTOR(384))
--     (verified against ODBC Driver 17, no double-cast needed); read-back
--     via CAST(embedding AS VARCHAR(MAX)) -> JSON array string.
--   * Full-Text Search is installed (IsFullTextInstalled = 1, 59 languages)
--     — see the DEFERRED note below; this fact is still true, just not
--     actioned in THIS migration.
--   * NO vector index: on-prem the DiskANN vector index is preview AND
--     makes the table read-only, which is incompatible with an
--     ingest-growing corpus; Microsoft recommends exact search under 50k
--     vectors and the corpus is ~23k chunks. Retrieval therefore uses an
--     exact VECTOR_DISTANCE table scan (Slice 2 application code, not
--     schema) — this migration deliberately creates NO vector index.
--
-- ----------------------------------------------------------------------
-- DEFERRED: full-text catalog + index (Phase 1 amendment, live-apply
-- failure)
-- ----------------------------------------------------------------------
-- This migration originally (author-time) included a third batch creating
-- CREATE FULLTEXT CATALOG memory_ft_catalog + CREATE FULLTEXT INDEX ON
-- memory.chunks(chunk_text) ... WITH CHANGE_TRACKING AUTO. A live apply
-- attempt against 10.42.0.249 FAILED:
--   pyodbc.ProgrammingError ('42000', ...CREATE FULLTEXT CATALOG statement
--   cannot be used inside a user transaction. (574))
-- ROOT CAUSE: database/migrate.py's apply_migration() runs every batch of a
-- migration file inside ONE user transaction (autocommit off, single commit
-- at the end, rollback on any failure) — but SQL Server's CREATE FULLTEXT
-- CATALOG and CREATE FULLTEXT INDEX are DDL operations that cannot run
-- inside a user transaction at all; they require autocommit. This is
-- fundamentally incompatible with migrate.py's current transactional model,
-- not a batch-ordering problem GO could fix. The failed apply rolled back
-- cleanly (verified: no memory schema/tables/catalog exist on the live DB;
-- 008 was never recorded in ops.schema_migrations), so amending this file
-- in place — rather than shipping a corrective follow-up migration — is
-- safe.
-- The full-text catalog + index are therefore DEFERRED to a future
-- migration 009 that will need migrate.py to gain NON-TRANSACTIONAL
-- migration support (tracked separately; a card will be created by the
-- lead). This is acceptable to defer: memory.documents + memory.chunks
-- (this migration) are everything the ingest write-path and the one-time
-- local-corpus ETL need; full-text (the lexical half of hybrid retrieval)
-- is only required later, by the Slice 2 QUERY path.
--
-- ----------------------------------------------------------------------
-- Batch model (read database/migrate.py before editing this file)
-- ----------------------------------------------------------------------
-- database.migrate.split_batches() splits migration text on standalone
-- "GO" lines (case-insensitive, alone on its own line) into batches; each
-- batch is sent as its own pyodbc cursor.execute() call, but ALL batches in
-- a file run inside ONE transaction — apply_migration() commits once after
-- every batch AND the ops.schema_migrations bookkeeping INSERT succeed, and
-- rolls back the whole file on any failure. pyodbc itself never sees a
-- literal "GO" (that token is a sqlcmd/SSMS-only separator); the runner
-- strips it before executing. This entire file therefore runs inside ONE
-- transaction, which is exactly why the full-text batch was removed (see
-- DEFERRED note above — CREATE FULLTEXT CATALOG/INDEX cannot run inside any
-- user transaction, regardless of batch placement). This file uses TWO
-- GO-delimited batches, both fully transactional-safe:
--
--   1. Schema creation. CREATE SCHEMA must be the first statement in a
--      batch; wrapping it in EXEC() of a dynamic string (001's idiom) gives
--      it its own implicit batch, so it can sit inside an IF NOT EXISTS
--      guard without needing to literally be first. No bare GO is needed
--      to isolate this from the surrounding IF block (EXEC() already
--      isolates it) — but a GO still follows it below, to guarantee the
--      "memory" schema (created only at runtime, via dynamic SQL) is
--      visible to batch 2's schema-qualified CREATE TABLE statements. DDL
--      changes within a transaction are visible to later statements in the
--      SAME transaction even before commit, but only across a batch
--      boundary — not necessarily to the SAME literal batch that (via
--      EXEC) created them — so the GO plays it safe.
--   2. Both tables + all their indexes, in one batch. A plain CREATE TABLE
--      (unlike ALTER TABLE ADD COLUMN — see migration 004's GO before the
--      index on its new ingest_seq column) needs no batch isolation from
--      statements that reference it LATER in the SAME batch:
--      memory.chunks' FK to memory.documents and every CREATE INDEX here
--      reference objects defined earlier in this same batch, exactly like
--      registry.machines + registry.repositories (with its FK) in
--      002_registry_tables.sql, or registry.tasks + its two indexes in
--      006_central_tasks.sql. Neither table needs its own inner GO. This is
--      the LAST batch in the file — no trailing GO.
--
-- ----------------------------------------------------------------------
-- Conventions mirrored from 001-007 (database/migrate.py module docstring,
-- database/README.md):
--   * Every prefixed-ULID id column is NVARCHAR(64) uniformly.
--   * DATETIMEOFFSET + SYSDATETIMEOFFSET() default for every UTC timestamp.
--   * source_type is APP-VALIDATED (mirrors mcp/memory/adapters/base.py
--     VALID_SOURCE_TYPES), NOT a DB CHECK — migration 006 cites this exact
--     memory-subsystem precedent when doing the same for
--     registry.tasks.status.
--   * No FK from source_machine_id back to registry.machines — mirrors
--     ops.events / registry.tasks.originating_client: a document ingested
--     from a machine that raced ahead of (or predates, or never has) a
--     registry row must not be blocked or cascade-deleted by registry
--     churn.
--   * Named PK/FK/UNIQUE/DEFAULT constraints throughout.
--   * JSON payload columns (memory.chunks.meta) are NVARCHAR(MAX), not the
--     native `json` type — same pyodbc-driver-simplicity deferral as
--     ops.events.payload / registry.agent_sessions.capabilities.
--
-- memory.chunks -> memory.documents ON DELETE CASCADE is the FIRST cascade
-- in this database (every existing FK elsewhere is plain NO ACTION) —
-- intentional and lead-approved (card c8e686a9): a content-changed or
-- explicitly-removed document must delete its chunk rows atomically at the
-- schema level, mirroring MemoryStore._delete_document's transactional
-- chunks_vec+chunks+documents delete in the local (SQLite) store.
--
-- Schema choice: ONE `memory` schema for both tables, per the epic card's
-- explicit ask (a finer work/memory/content/vector/ops split was an early,
-- non-binding sketch in docs/architecture/PROPOSAL.md, superseded by the
-- epic card's locked description).
--
-- Deliberately NOT included here:
--   * Full-text catalog + index — DEFERRED to migration 009, see the
--     "DEFERRED" header note above (not simply out of scope: it was
--     authored here originally and removed after a failed live apply).
--   * No stored procedure/function for VECTOR_DISTANCE query composition —
--     that is Slice 2 application code (services/api/store.py), not schema.
--   * No agent-writable memory / edges (later phase, out of this epic).
-- ============================================================================


-- ---------------------------------------------------------------------------
-- Batch 1 of 2: schema
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'memory')
BEGIN
    EXEC('CREATE SCHEMA memory');
END
GO


-- ---------------------------------------------------------------------------
-- Batch 2 of 2: tables + indexes
-- ---------------------------------------------------------------------------

-- memory.documents — one row per distinct ingested document.
--
-- document_id: two populations share this plain NVARCHAR(64) column — an
-- ETL'd row (the one-time local-corpus migration, a later slice) keeps its
-- EXISTING local id verbatim (mcp/memory/adapters/base.py's
-- `_doc_id(prefix, key)`, e.g. "chatgpt_1a2b3c4d5e6f7890"); a NEW
-- hub-ingested row mints a fresh `mem_<ULID>` via
-- contracts.identifiers.new_id(MEMORY) (the "mem" prefix already exists in
-- KNOWN_PREFIXES and is unused elsewhere).
CREATE TABLE memory.documents (
    document_id       NVARCHAR(64)    NOT NULL,
    title             NVARCHAR(1000)  NULL,
    -- App-validated against mcp/memory/adapters/base.py VALID_SOURCE_TYPES
    -- ("document", "chatgpt_conversation", "news") — no DB CHECK, see header.
    source_type       NVARCHAR(50)    NOT NULL,
    -- Client-side origin: an absolute path (prose) or a stable conversation
    -- id (ChatGPT). NULL when no stable client identity is known.
    source_path       NVARCHAR(1000)  NULL,
    -- sha256 hex digest of the raw document bytes (mirrors local
    -- documents.content_hash) — the actual dedup key, see
    -- UQ_memory_documents_content_hash below.
    content_hash      NVARCHAR(64)    NOT NULL,
    -- Which machine submitted the ingest that produced/last-touched this
    -- row. No FK — see header note (must accept a document from a machine
    -- that has not yet, or will never, call /v1/machines/register).
    source_machine_id NVARCHAR(64)    NULL,
    -- 'uploaded' (native hub ingest) vs. a future 'etl' tag for the
    -- one-time corpus migration — distinguishes an ETL'd row from a
    -- natively hub-ingested one without a separate boolean column.
    provenance        NVARCHAR(50)    NOT NULL
        CONSTRAINT DF_memory_documents_provenance DEFAULT (N'uploaded'),
    -- ISO date or date+time string, kept as free text (not DATE /
    -- DATETIMEOFFSET) to byte-for-byte preserve the local store's
    -- substr-prefix date-range filter semantics
    -- (mcp/memory/store.py._doc_filter_sql) without an ETL-time parsing/
    -- coercion step. NULL falls back to created_at at query time.
    published_at      NVARCHAR(32)    NULL,
    created_at        DATETIMEOFFSET  NOT NULL
        CONSTRAINT DF_memory_documents_created_at DEFAULT (SYSDATETIMEOFFSET()),
    updated_at        DATETIMEOFFSET  NOT NULL
        CONSTRAINT DF_memory_documents_updated_at DEFAULT (SYSDATETIMEOFFSET()),
    CONSTRAINT PK_memory_documents PRIMARY KEY (document_id)
);

-- Dedup key (card's explicit ask): global content-addressed uniqueness,
-- stricter than local's per-doc_id-identity dedup.
CREATE UNIQUE INDEX UQ_memory_documents_content_hash
    ON memory.documents (content_hash);

-- Filter pushdown for source_type (mirrors local idx_documents_source_type;
-- memory_query's source_type filter pushes into SQL here).
CREATE INDEX IX_memory_documents_source_type
    ON memory.documents (source_type);

-- Provenance / audit lookups ("what did machine X ingest").
CREATE INDEX IX_memory_documents_source_machine
    ON memory.documents (source_machine_id);


-- memory.chunks — one row per chunk of a document, with its embedding.
--
-- Two-key shape, deliberately mirroring the local schema's
-- `id INTEGER PRIMARY KEY` (physical rowid, what chunks_vec keys on) /
-- `chunk_id TEXT UNIQUE` (business key, what the API/tool response and RRF
-- fusion operate on) split:
--   * chunk_seq_id BIGINT IDENTITY — physical PK, monotonic in insertion
--     order (mirrors ops.events.ingest_seq).
--   * chunk_id NVARCHAR(64) UNIQUE — the external business key,
--     "mem:<document_id>:<seq>", byte-identical to the local scheme
--     (mcp/memory/store.py._insert_chunks: f"mem:{doc.doc_id}:{chunk.seq}").
CREATE TABLE memory.chunks (
    chunk_seq_id     BIGINT          IDENTITY(1,1) NOT NULL,
    chunk_id         NVARCHAR(64)    NOT NULL,
    document_id      NVARCHAR(64)    NOT NULL,
    seq              INT             NOT NULL,
    -- Named chunk_text (not `text`) to avoid any confusion with the
    -- deprecated SQL Server `text` TYPE keyword; ordinary NVARCHAR(MAX).
    chunk_text       NVARCHAR(MAX)   NOT NULL,
    -- JSON blob (chunk-level metadata, e.g. ChatGPT speaker role) —
    -- NVARCHAR(MAX), same driver-simplicity rationale as ops.events.payload.
    meta             NVARCHAR(MAX)   NULL,
    -- Per-row model name + dim (migration insurance — mirrors local
    -- exactly): lets a future model change be detected/filtered per row
    -- instead of only at the whole-database level.
    embedding_model  NVARCHAR(200)   NOT NULL,
    embedding_dim    INT             NOT NULL,
    -- CONFIRMED (card c8e686a9, live probe): VECTOR(384) is valid SQL
    -- Server 2025 DDL; float32 storage, dimension enforced by the type.
    embedding        VECTOR(384)     NOT NULL,
    created_at       DATETIMEOFFSET  NOT NULL
        CONSTRAINT DF_memory_chunks_created_at DEFAULT (SYSDATETIMEOFFSET()),
    CONSTRAINT PK_memory_chunks PRIMARY KEY (chunk_seq_id),
    CONSTRAINT UQ_memory_chunks_chunk_id UNIQUE (chunk_id),
    -- ON DELETE CASCADE — the FIRST cascade in this database; see header
    -- note. memory.chunks -> memory.documents IS a real delete
    -- relationship (a content-changed document, or an explicit removal,
    -- deletes its chunk rows) — CASCADE keeps that atomic at the schema
    -- level instead of requiring the app to delete chunks first at every
    -- call site.
    CONSTRAINT FK_memory_chunks_document FOREIGN KEY (document_id)
        REFERENCES memory.documents (document_id) ON DELETE CASCADE
);

-- Per-document chunk listing / cascade-delete planning (mirrors local
-- idx_chunks_doc_id).
CREATE INDEX IX_memory_chunks_document_id
    ON memory.chunks (document_id);
-- No trailing GO: batch 2 is the last batch in this file. The full-text
-- catalog + index that would have followed here are DEFERRED to migration
-- 009 — see the "DEFERRED" header note above.
