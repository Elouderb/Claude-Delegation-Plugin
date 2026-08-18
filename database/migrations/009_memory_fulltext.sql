-- Migration 009: memory full-text catalog + index (Slice 2 prerequisite)
-- (epic 6fead93b, card eb45271b — follow-up to migration 008's DEFERRED
-- full-text section; read that file's header before this one).
--
-- migrate:no-transaction
--
-- ----------------------------------------------------------------------
-- WHY NON-TRANSACTIONAL (the directive above)
-- ----------------------------------------------------------------------
-- CREATE FULLTEXT CATALOG and CREATE FULLTEXT INDEX are DDL SQL Server
-- refuses to run inside a user transaction AT ALL. A live apply attempt
-- of this exact pair of statements, when they were still part of
-- migration 008, failed with:
--   pyodbc.ProgrammingError ('42000', '[42000] ... CREATE FULLTEXT
--   CATALOG statement cannot be used inside a user transaction. (574)')
-- (see 008's "DEFERRED" header note for the full trail). Confirmed
-- against current Microsoft docs while authoring this file: the
-- CREATE FULLTEXT INDEX reference doc states the same restriction for
-- the index statement too ("CREATE FULLTEXT INDEX can't be placed inside
-- a user transaction. This statement must be run in its own implicit
-- transaction.") — both statements below need it, not just the catalog.
--   https://learn.microsoft.com/en-us/sql/t-sql/statements/create-fulltext-catalog-transact-sql
--   https://learn.microsoft.com/en-us/sql/t-sql/statements/create-fulltext-index-transact-sql
--
-- database/migrate.py wraps every migration file in ONE transaction by
-- default (autocommit off, single commit/rollback per file) — fundamentally
-- incompatible with that restriction. Card eb45271b added a per-file
-- opt-out: the ``-- migrate:no-transaction`` header directive above
-- (parsed by database.migrate._parses_no_transaction_directive, matched
-- anywhere in this leading header, not just line 1) makes
-- apply_migration() run this file with the connection in AUTOCOMMIT
-- instead of one wrapping transaction — see
-- database.migrate._apply_migration_autocommit.
--
-- THERE IS NO ROLLBACK ON THIS PATH. If the index batch (below) fails
-- after the catalog batch already committed, the catalog permanently
-- exists and the ops.schema_migrations bookkeeping row is NOT recorded
-- (this file stays "pending"). A re-run must therefore be safe — which is
-- exactly why BOTH statements below are guarded with IF NOT EXISTS
-- against sys.fulltext_catalogs / sys.fulltext_indexes: re-applying this
-- file after a partial failure (or after it's already fully applied) is a
-- no-op for whichever object already exists, and only creates what's
-- still missing.
--
-- ----------------------------------------------------------------------
-- BATCH LAYOUT
-- ----------------------------------------------------------------------
-- Confirmed while authoring this file: unlike CREATE SCHEMA/PROCEDURE/
-- VIEW/TRIGGER/FUNCTION, neither CREATE FULLTEXT CATALOG nor CREATE
-- FULLTEXT INDEX is required to be the first/only statement in a batch —
-- Microsoft's own CREATE FULLTEXT INDEX example runs a unique index,
-- then CREATE FULLTEXT CATALOG, then CREATE FULLTEXT INDEX all in ONE
-- batch, no GO between them. This file still uses TWO GO-delimited
-- batches below as a deliberate belt-and-suspenders choice (not a
-- technical requirement): each batch is one autocommit unit, so the
-- catalog is guaranteed fully committed and visible before the index
-- batch even begins, and a partial failure has the simplest possible
-- story (catalog exists, index doesn't yet — re-run finishes the job).
--   1. CREATE FULLTEXT CATALOG memory_ft_catalog, IF NOT EXISTS-guarded.
--   2. CREATE FULLTEXT INDEX ON memory.chunks(chunk_text) ... ON
--      memory_ft_catalog, IF NOT EXISTS-guarded.
--
-- ----------------------------------------------------------------------
-- KEY INDEX REQUIREMENT
-- ----------------------------------------------------------------------
-- A full-text index's KEY INDEX must be a single-column, unique,
-- non-nullable index on the table (integer recommended). Confirmed live
-- against the db graph (db_get_table memory.chunks) before authoring this
-- migration: PK_memory_chunks (migration 008) is exactly that —
-- CONSTRAINT PK_memory_chunks PRIMARY KEY (chunk_seq_id), with
-- chunk_seq_id BIGINT IDENTITY(1,1) NOT NULL as the table's sole PK
-- column. It qualifies with no further schema change needed.
--
-- ----------------------------------------------------------------------
-- WHY NOW / SEQUENCING
-- ----------------------------------------------------------------------
-- Full-text backs ONLY the lexical half of hybrid retrieval (Slice 2
-- /v1/memory/query). Migration 008 (tables) plus the one-time corpus ETL
-- already loaded memory.chunks (23,334 rows at authoring time) with no
-- full-text index present, so CHANGE_TRACKING AUTO here performs the
-- initial population as soon as this migration is applied — no separate
-- backfill step required.
--
-- This migration is authored, NOT applied, by the authoring agent (card
-- eb45271b explicitly forbids any live-DB apply here). The lead applies
-- it later (gated), exercising migrate.py's autocommit path for the
-- first time in this database's migration history. Flag for that apply
-- step: confirm the DB login migrate.py runs as (agent_write_login) holds
-- CREATE FULLTEXT CATALOG permission before applying — it created
-- schemas/tables in migrations 001-008, so it is likely db_owner-level,
-- but this has not been independently verified against sys.fulltext
-- permissions specifically.
--
-- Deliberately NOT included here:
--   * Any Slice 2 application code (query composition against the new
--     full-text index) — that is services/api/*, not schema.
--   * A vector index — migration 008 already deliberately omits one (see
--     that file's header); unrelated to full-text and unchanged here.
-- ============================================================================


-- ---------------------------------------------------------------------------
-- Batch 1 of 2: full-text catalog
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.fulltext_catalogs WHERE name = 'memory_ft_catalog')
BEGIN
    CREATE FULLTEXT CATALOG memory_ft_catalog;
END
GO


-- ---------------------------------------------------------------------------
-- Batch 2 of 2: full-text index on memory.chunks(chunk_text)
-- ---------------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM sys.fulltext_indexes WHERE object_id = OBJECT_ID('memory.chunks'))
BEGIN
    CREATE FULLTEXT INDEX ON memory.chunks(chunk_text)
        KEY INDEX PK_memory_chunks
        ON memory_ft_catalog
        WITH CHANGE_TRACKING AUTO;
END
-- No trailing GO: batch 2 is the last batch in this file.
