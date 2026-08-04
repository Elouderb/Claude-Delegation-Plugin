-- Migration 001: create the registry and ops schema groups.
--
-- `ops` already exists by the time this file runs — the runner's own
-- bootstrap step (database/migrate.py ensure_bootstrap()) creates schema
-- `ops` + table `ops.schema_migrations` before any migration file is
-- evaluated, because a migration can only be *recorded* once that table
-- exists. This migration still (idempotently) asserts both schemas exist so
-- schema creation stays visible in the versioned migration history rather
-- than being hidden entirely inside runner-internal bootstrap code.
--
-- CREATE SCHEMA must be the first statement in a batch; wrapping it in
-- EXEC() of a dynamic string gives it its own implicit batch, which is the
-- standard way to make a guarded CREATE SCHEMA idempotent without a
-- sqlcmd/SSMS "GO" batch separator.

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'registry')
BEGIN
    EXEC('CREATE SCHEMA registry');
END

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'ops')
BEGIN
    EXEC('CREATE SCHEMA ops');
END
