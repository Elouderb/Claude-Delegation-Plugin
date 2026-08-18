# Agent OS Central Store — Migrations (Phase 1a)

Migration runner + versioned SQL migrations that stand up the `registry.*`
and `ops.*` schema groups in the central SQL Server database
(`agent_os_memory`) — proposal §17 first slice. See epic card `78a6ac11`
(comment 131) and card `6f83ab1f` for the full spec and environment facts.

**Scope**: `registry.*` (machine/repository/agent-session identity) and
`ops.*` (event log + sync/ingest bookkeeping) only. The `work`/`memory`/
`content`/`vector` schema groups, the Agent OS API service, and the outbox are
later phases in the epic — not here.

## Connection

The connection string lives ONLY in the `AGENT_OS_CENTRAL_DB_CONNECTION_STRING`
environment variable (`.env`, gitignored). Never hardcode, print, or commit
it. `database/migrate.py` loads `.env` via `python-dotenv` the same way
`mcp/db_tools/build_db_graph.py` does.

This is a **different** database from the one the `db_*` MCP graph tools
watch (`agent_memory`, via `DB_CONNECTION_STRING`) — do not conflate the two.

## Usage

```bash
python -m database.migrate --status     # report only, read-only
python -m database.migrate --dry-run    # show what would be applied, read-only
python -m database.migrate              # apply pending migrations (the default action)
```

Requires `pyodbc` (`mcp/db_requirements.txt`) and a live SQL Server reachable
via the connection string above.

## Migrations

Numbered `database/migrations/NNN_description.sql` files, applied in order:

- `001_create_schemas.sql` — `registry` and `ops` schemas (idempotent; `ops`
  itself is actually created earlier by the runner's bootstrap step — see
  below).
- `002_registry_tables.sql` — `registry.machines`, `registry.repositories`
  (with the `repo_root_hash` natural key, below), `registry.agent_sessions`.
- `003_ops_tables.sql` — `ops.events` (+ indexes), `ops.sync_cursors`,
  `ops.ingest_errors`.
- `004_events_seq_sessions_ended.sql` — `ops.events.ingest_seq`
  (`BIGINT IDENTITY`, + unique DESC index) and an `occurred_at`-leading index;
  `registry.agent_sessions.ended_at`. See the design notes below.
- `005_sync_cursors_seq.sql` — `ops.sync_cursors.last_seq` (`BIGINT NULL`), the
  numeric replay position `GET/PUT /v1/sync/{client_id}` (card `93baf05b`)
  reads/writes. See the design notes below.
- `006_central_tasks.sql` — `registry.tasks`, the central down-projection read
  model for a repository-local card (Phase 2a, card `ffcaf548`). No FK back to
  `registry.repositories`, mirroring `ops.events`'s rationale. See the design
  notes below.
- `007_machine_credentials.sql` — `registry.machine_credentials`: per-machine
  API keys for the central `/v1` API (card `8737706e`). Only a SHA-256 hash of
  each key's secret half is ever stored; revocation is a soft delete
  (`revoked_at`) so a `key_id` can never be re-minted.
- `008_memory_schema.sql` — the `memory` schema plus `memory.documents` /
  `memory.chunks`, the central RAG corpus (epic `6fead93b`, card `c8e686a9`).
  Tables + indexes only — the full-text catalog/index originally drafted here
  had to be DEFERRED (see `009` below) after a live apply failed: SQL Server
  refuses `CREATE FULLTEXT CATALOG` inside a user transaction, and every
  migration up to this point (this file included) ran inside one.
- `009_memory_fulltext.sql` — `memory_ft_catalog` + a full-text index on
  `memory.chunks(chunk_text)` (`KEY INDEX PK_memory_chunks`,
  `WITH CHANGE_TRACKING AUTO`), backing the lexical half of Slice 2 hybrid
  retrieval (card `eb45271b`). The FIRST migration to use the
  `-- migrate:no-transaction` opt-out described next — required because
  `CREATE FULLTEXT CATALOG`/`CREATE FULLTEXT INDEX` cannot run inside a user
  transaction at all.

Each migration is applied **transactionally by default** (all its batches +
the bookkeeping row, or nothing) and **checksummed** (sha256 of the file
text) so editing an already-applied migration is detected —
`apply_pending()` refuses to proceed rather than silently re-applying or
ignoring the drift. Re-running against an up-to-date database is a no-op.

**Opting out of the transaction wrapper.** Some SQL Server DDL (`CREATE
FULLTEXT CATALOG`/`CREATE FULLTEXT INDEX`, `CREATE DATABASE`, `ALTER
DATABASE ... SET`, backup/restore, ...) cannot run inside a user transaction
at all — SQL Server raises error 574 (SQLSTATE 42000) if you try, which is
exactly what forced `009`'s full-text objects out of `008` (see above). A
migration file opts out with a `-- migrate:no-transaction` header directive —
a standalone comment line matching that exact text anywhere in the file's
leading header (blank lines, `--` comments, and `GO` separators before the
first real SQL statement; not required to be literally line 1). Such a
migration runs every batch, and the bookkeeping row, with the connection in
**autocommit** instead: each statement commits itself immediately, and
**there is no rollback**. A partial failure leaves every batch executed
before it permanently applied and the migration itself unrecorded (still
"pending" — safe to re-run). Every no-transaction migration must therefore be
written **idempotently** (an `IF NOT EXISTS`/`IF EXISTS` guard on every
statement), as `009` is. Migrations `001`-`008` carry no directive and are
fully transactional, unaffected by this option.

### Runner bookkeeping (`ops.schema_migrations`)

The runner creates schema `ops` and table `ops.schema_migrations` itself,
idempotently, **before** evaluating the migration set (see
`ensure_bootstrap()` in `migrate.py`) — a migration can only be *recorded*
once that table exists, so it can't record its own creation. This mirrors how
Flyway/Alembic self-manage their own metadata table. `--status` and
`--dry-run` never call this bootstrap step, so they stay read-only even
against a virgin database (a missing `ops.schema_migrations` just means every
migration reports "pending").

## Design decisions (documented, not left implicit)

- **ID column width**: every prefixed-ULID id column (`machine_id`,
  `repository_id`, `session_id`, `event_id`, ...) is `NVARCHAR(64)`
  uniformly, rather than an exact per-prefix width (e.g. `mach_` + 26 = 31,
  `evt_` + 26 = 30 today). A future longer prefix therefore never forces a
  migration.
- **JSON payload columns** (`capabilities`, `payload`): `NVARCHAR(MAX)`, not
  SQL Server 2025's native `json` type — kept for pyodbc driver simplicity
  this phase. **Revisit** once the native-json bind/fetch path is verified
  with this driver stack.
- **UTC timestamps**: `DATETIMEOFFSET` everywhere. pyodbc does **not**
  natively decode this wire type (ODBC SQL type `-155` /
  `SQL_SS_TIMESTAMPOFFSET`) — verified live against pyodbc 5.3.0 + ODBC
  Driver 17: fetching one raises `ODBC SQL type -155 is not yet supported`
  without an output converter. `database.migrate.connect()` always registers
  one; any other code reading these columns must do the same (see
  `_decode_datetimeoffset` in `migrate.py`).
- **No FKs from `ops.events`/`ops.ingest_errors`/`ops.sync_cursors` back to
  `registry.*`**: an event/error/cursor log must accept a row for a
  machine/repository/session that raced ahead of, predates, or outlives its
  registry row, and must never be blocked or cascade-deleted by registry
  churn. Referential integrity for these ids is an API-layer concern for a
  later phase.
- **`registry.repositories` natural key**: a `PERSISTED` computed column
  `repo_root_hash AS CONVERT(BINARY(32), HASHBYTES('SHA2_256', repo_root))`
  backs a `UNIQUE (machine_id, repo_root_hash)` index. `repo_root` is
  `NVARCHAR(1000)` (2000 bytes), which exceeds SQL Server's 1700-byte index key
  limit and so cannot be indexed directly; the 32-byte hash can. This prevents a
  machine accumulating *duplicate* rows for the same checkout path when
  `.agent-os` is deleted (`git clean -fdx`) and the repo id is re-minted (the
  API layer `UPSERT`s on this key; the schema constraint makes a duplicate
  impossible either way). **API lookups must compare on this same hash
  expression**, not a plain `repo_root = ?` string equality — this DB's collation
  is case-insensitive (`SQL_Latin1_General_CP1_CI_AS`), so a string compare would
  wrongly match two *different* checkouts differing only by case; `services.api.store`
  matches with `repo_root_hash = CONVERT(BINARY(32), HASHBYTES('SHA2_256', ?))`.
- **`ops.events.ingest_seq` (migration `004`)**: a `BIGINT IDENTITY`, unique and
  monotonic in central-arrival order, with a unique DESC index. It gives the
  event log a stable *total* order to page over — `occurred_at` alone has no
  unique tiebreaker, so keyset pagination was impossible. `GET /v1/events/recent`
  orders by `ingest_seq DESC` and pages with an `after_seq` cursor +
  `next_after` continuation token. Adding the `IDENTITY` to the already-populated
  table backfills the existing rows in a stable order.
- **`registry.agent_sessions.ended_at` (migration `004`)**: set when a session
  finishes. The API materializes/closes session rows from `agent.started` /
  `agent.finished` events; `agent.finished` stamps `ended_at`. `NULL` for a
  running or never-closed session.
- **`ops.sync_cursors` (decision made in 1d, card `93baf05b`)**: created in `003`
  for a per-consumer replay position; the 1c drain keeps its OWN cursor in a
  local file (`.agent-os/sync/cursor.json`, unaffected), but 1d's stand-in M3
  client (`services/m3_client`) is the first SERVER-SIDE puller, and it needs to
  persist its `--follow` position across process restarts — exactly the need
  this table exists for — so the decision is **implement**, not drop.
  `GET/PUT /v1/sync/{client_id}` (`services/api/app.py`) upsert on the table's
  PRIMARY KEY `client_id`, the same PK-upsert pattern as `registry.machines`.
  One wrinkle forced migration `005`: `003`'s `last_event_id` (an opaque ULID
  string) carries no ordering information by itself and cannot drive
  `GET /v1/events/recent`'s keyset pagination, which pages on `ops.events.
  ingest_seq` (added later, in `004`) — a client that only recorded
  `last_event_id` would have no numeric position to resume `?after_seq=` from.
  `005` adds `last_seq BIGINT NULL` as the AUTHORITATIVE resume position;
  `last_event_id` is still stored (in lockstep) purely for a human glancing at
  the row to see which event that position corresponds to, and is never used to
  compute a resume point. `GET` of an unknown `client_id` returns
  `{"last_seq": null, "last_event_id": null, "updated_at": null}` (200, not 404)
  so a fresh consumer doesn't need a special case to "start from now".
- **`occurred_at` bound as UTC**: `database.events` normalizes an
  `EventEnvelope.occurred_at` to UTC (`.astimezone(timezone.utc)`) *before*
  binding it. pyodbc sends an aware datetime's wall-clock digits with a `+00:00`
  offset to `DATETIMEOFFSET` — it does **not** carry the datetime's own offset —
  so a non-UTC `occurred_at` would otherwise be stored as a different instant
  (verified live). Normalizing first keeps the stored moment correct.

## Files

- `migrate.py` — the runner (discovery, checksumming, transactional-by-default
  apply with a per-file `-- migrate:no-transaction` autocommit opt-out,
  status/dry-run, CLI).
- `events.py` — minimal `EventEnvelope` <-> `ops.events` row mapping (used by the
  live verification test, not a real ingestion API — that's later). Provides
  `insert_event_envelope` (raw) and `insert_event_if_absent` (idempotent, via
  `WHERE NOT EXISTS`) so the Phase 1c drain's normal duplicate-delivery retry —
  a crash between ack and cursor advance re-presents an already-stored event — is
  a harmless no-op rather than a PK-violation batch poison.
- `migrations/*.sql` — the versioned migrations described above.
