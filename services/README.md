# Agent OS API service (`/v1`) + outbox drain + M3 stand-in client — Phase 1c/1d

The control-plane read/write surface in front of the central store
(`agent_os_memory`), the plugin-side worker that ships local events to it, and a
small read-only terminal client that plays Machine 3's role until the real M3
VM exists. Closes the §17 first slice: machine/repo/session registration,
heartbeats, and card/graph/lifecycle events flow from any repo into the central
DB, surviving network loss — and are now human-visible end to end. Part of epic
`78a6ac11` (cards `8f84948b` Phase 1c, `93baf05b` Phase 1d); builds on the
Phase 1a migrations (`database/`) and Phase 1b outbox (`outbox/`).

## Components

- **`services/api/`** — a Flask app (`create_app`) exposing versioned `/v1`
  endpoints, backed by an injectable store (`SqlStore` over
  `database.migrate.connect`, or an in-memory `FakeStore` for tests).
- **`outbox/drain.py`** — the drain worker: registers identity, then ships
  outbox events to the API with a safe byte-offset cursor.
- **`mcp/sync_server.py`** — spawns the drain as an optional server companion
  (the way `graph_server` spawns the graph UI). OFF by default.
- **`services/m3_client/`** — a read-only stand-in for Machine 3 (Phase 1d):
  `python -m services.m3_client status[--follow][--json]` renders machines,
  repositories, active sessions, and recent events from the `/v1` API. See
  **M3 stand-in client** below.

## Running (the three-terminal demo)

```bash
# Terminal 1: API service (defaults to 127.0.0.1:8765)
python -m services.api

# Terminal 2: drain this repo's outbox once (tests / manual), or run the loop
python -m outbox.drain --once
python -m outbox.drain

# Terminal 3: the M3 stand-in client — watch the control plane fill in live
python -m services.m3_client status --follow
```

Both read configuration from the environment / `.env` (see `.env.example`). The
API needs `flask` (core) + `pyodbc` (the database extra) + `AGENT_OS_API_KEY` +
`AGENT_OS_CENTRAL_DB_CONNECTION_STRING`. The drain's **transport is stdlib-only**
(`urllib`, no `requests`) and Windows-safe; loading machine/repo identity uses the
bundled top-level `contracts` package (pydantic) — the same models the rest of the
plugin uses. If `contracts` can't be imported the drain **fails fast** with a clear
message (a bare-Python checkout without the plugin's own packages is a
misconfiguration, not a transient outage). The server companion puts the plugin
root on the drain child's `PYTHONPATH`, so `python -m outbox.drain` resolves those
packages regardless of the child's working directory.

## Endpoints (all `/v1`, JSON)

| Method + path | Purpose |
|---|---|
| `POST /v1/machines/register` | Upsert `registry.machines` (PK `machine_id`). |
| `POST /v1/repositories/register` | Upsert `registry.repositories` on the **natural key** `(machine_id, repo_root)` — see below. |
| `POST /v1/sessions/register` | Upsert `registry.agent_sessions` (PK `session_id`). |
| `POST /v1/sessions/{id}/heartbeat` | Refresh `last_heartbeat_at` (+ optional status/current task); `404` if unknown. Transport ack is `{"result":"ok", …}` — `result`, not `status`, so it never collides with a resource's own lifecycle `status`. |
| `POST /v1/events/batch` | Ingest `{events: [envelope…]}` (**max 500 events** → `413` over the cap); per-event `accepted` / `duplicate` / `rejected`. Set-based ingest (one `SELECT` + one `INSERT … OPENJSON`), not a per-event round trip. `agent.started`/`agent.finished` events also materialize/close a session row (see below). |
| `GET /v1/machines` | List machines. |
| `GET /v1/repositories[?machine_id=]` | List repositories. |
| `GET /v1/sessions[?active=1]` | List sessions (`active=1` → status in starting/running/waiting). Reflects **real** agents, not only the drain's own session, because agent lifecycle events materialize session rows. |
| `GET /v1/events/recent?limit=&repository_id=&event_type=&after_seq=` | Recent events, newest first — see **Pagination** below. |
| `GET /v1/sync/{client_id}` | A consumer's replay position (`{"client_id", "last_seq", "last_event_id", "updated_at"}`). Unknown `client_id` → `200` with all three fields `null` (not `404`) — a fresh consumer needs no special case. |
| `PUT /v1/sync/{client_id}` | Upsert that position — body `{"last_seq": int\|null, "last_event_id": str\|null}`. See **`ops.sync_cursors`** below. |
| `POST /v1/machines/{id}/credentials` | **Admin-only.** Issue a per-machine key. Optional body `{"label"}`. Returns `{"key_id", "secret", "key": "<key_id>.<secret>"}` — the secret is shown **once** and only its hash is stored. `404` if the machine is unknown. |
| `POST /v1/machines/{id}/credentials/{key_id}/revoke` | **Admin-only.** Revoke a key (idempotent). `404` if unknown for that machine. |
| `GET /v1/machines/{id}/credentials` | **Admin-only.** List a machine's key ids + status (`created_at`/`last_used_at`/`revoked_at`) — **never** secrets or hashes. |
| `GET /health` | Liveness only (no data, no DB) — the **one** unauthenticated route. |

**Auth (two-phase).** Every `/v1` route requires `Authorization: Bearer <token>`.
The token is resolved in two phases, both constant-time:

1. **Admin / bootstrap key** — the token is compared with `hmac.compare_digest`
   against `AGENT_OS_API_KEY`, which may be a single key OR a **comma-separated
   list** (every listed key is accepted, so a key rotates with an overlap window:
   add the new key, redeploy clients, then drop the old). The comparison does not
   short-circuit, so timing never reveals which listed key matched. An admin match
   is the full-authority identity used to register machines and issue per-machine
   keys; it **bypasses** machine binding. A server with no key configured fails
   closed (every request `401`).
2. **Per-machine key** — only if phase 1 fails and the token has the
   `<key_id>.<secret>` shape: the `key_id` is looked up `O(1)`, must exist and be
   un-revoked, and `SHA-256(secret)` must `hmac.compare_digest`-match the stored
   hash (only the hash is persisted — never the plaintext secret). A match binds
   the caller to that credential's `machine_id`.

**Machine binding.** A per-machine caller may act only as its own machine,
enforced against each resource's AUTHORITATIVE owner **at the store layer** — never
a client-supplied `machine_id` claim, which a caller could spoof or omit.
`register machine/repository/session` may neither create nor overwrite a resource
owned by another machine (`403`); `heartbeat` may target only a session the caller
owns (a cross-machine target is `404` — no mutation, no existence leak); and each
event's originating `machine_id` must equal the caller's bound machine (a foreign
event in a batch is rejected per-event, not a whole-batch failure). Admin callers
bypass binding. This closes the shared-key risk that any key-holder could overwrite
any machine's registry row.

> **Stopgap — sync cursors are admin-only.** `GET/PUT /v1/sync/<client_id>`
> (`ops.sync_cursors`) has no per-machine ownership column yet, so to stop a
> per-machine key from corrupting another consumer's replay position, both are
> **admin-only** for now. Every current sync consumer uses the shared admin key, so
> nothing is affected; proper first-writer-wins binding (card `52946159`) must land
> before any per-machine key is issued to a sync consumer. `AGENT_OS_API_KEY`'s
**value** on a client may now be EITHER the shared admin key OR a per-machine
`key_id.secret` — clients present it identically, and the server distinguishes by
format, so per-machine rollout is pure per-machine `.env` config with no client
code change. No credentials live in code.

Issue/rotate/revoke keys with the admin CLI `scripts/manage_keys.py` (run with the
admin `AGENT_OS_API_KEY`): `issue --machine-id … [--label …]`, `list --machine-id
…`, `revoke --machine-id … --key-id …`. **Rotation:** issue a new key for the
machine, update that machine's `AGENT_OS_API_KEY`, then revoke the old `key_id` —
the two overlap so there is no downtime. A lost secret is unrecoverable (only its
hash is stored): revoke the `key_id` and issue a new one.

> **Not yet: transport encryption (TLS).** This slice is the auth layer only. The
> API still speaks plain HTTP; a per-machine `key_id.secret` on the wire is only as
> private as the link. TLS termination (the M3 reverse proxy) is the separate
> Slice B — do not treat the LAN traffic as encrypted until it lands.

**Errors.** Every error path returns a uniform envelope
`{"error": {"code": "<machine_readable>", "message": "<human, sanitized>"}}` — no
stack traces, SQL, or connection details ever reach a response. Codes include
`unauthorized`, `forbidden`, `invalid_payload`, `invalid_status`, `not_found`,
`method_not_allowed`, `batch_too_large`, `payload_too_large`, `internal_error`.

**Posture.** Tolerant-reader inputs (bodies validated with the `contracts`
models, `extra="ignore"`), strict/explicit outputs. Each request opens one
short-lived DB connection via `database.migrate.connect` (which carries the
DATETIMEOFFSET converter). `threaded=True` is safe here — no shared mutable
state. A body larger than `MAX_CONTENT_LENGTH` (40 MiB) is rejected with `413`.

## Pagination (`GET /v1/events/recent`)

Events are ordered by `ingest_seq` — a server-assigned, unique, monotonic
sequence (migration `004`) — **not** `occurred_at`, which has no unique tiebreaker.
Each row carries its `ingest_seq` and `received_at`; the response also returns a
top-level `next_after` (the smallest `ingest_seq` on the page, or `null` when
caught up). A poller pages backwards by passing that value as `?after_seq=` on the
next request: it gets strictly-older events with **no overlap and no dropped
burst**, the property a 1d polling client needs. (Ordering on `occurred_at` alone
could not offer this — hence the `004` `ingest_seq` column.)

## Session read model

`agent.started` / `agent.finished` events materialize a `registry.agent_sessions`
row (started → `running`; finished → `finished` + `ended_at`, migration `004`), so
`GET /v1/sessions?active=1` answers §17's "list active agents" from real agent
activity, not only the drain's self-registration. Materialization runs on a
separate best-effort connection **after** the event batch commits: the event log
has no FK to `registry.*` (an event must always be recordable), but
`agent_sessions` does, so a session for a not-yet-registered machine is skipped
rather than allowed to poison the FK-free event ingest.

## Repository natural-key upsert (the subtle part)

`registry.repositories` has `UNIQUE(machine_id, repo_root_hash)` where
`repo_root_hash` is a PERSISTED `SHA2_256(repo_root)` computed column (card
`6f83ab1f` finding 8). When a checkout's `.agent-os/` is deleted (e.g.
`git clean -fdx`) and its `repository_id` is re-minted, a re-registration arrives
with a **new** `repository_id` but the **same** `(machine_id, repo_root)`. The
API **updates the existing row and returns its original `repository_id` as the
canonical id**, with `remapped: true` in the response so the client can reconcile
its local id — it never inserts a duplicate. (Concurrent first-registrations that
race the unique index are reconciled the same way.)

## Drain cursor discipline

Per batch the worker POSTs the valid events, treats `accepted`/`duplicate` as
resolved and marks `rejected` (preserved) for the dead-letter log, and advances
the cursor to the batch's last byte offset **only** after HTTP 200 with a
complete, index-aligned result set. Any transport failure — connection refused,
timeout, non-200, or a malformed/underlength response — advances the cursor by
nothing and triggers exponential backoff with jitter, so a network outage costs a
retry, never a lost or double-recorded event (at-least-once redelivery is
idempotent on `event_id`).

Dead-lettering (both malformed outbox lines and server-`rejected` events) is
**deferred until the batch is durably resolved**, right before the cursor advance
— so a batch that fails transport and is retried produces **exactly one**
dead-letter record per bad line, never one per retry. The read is also bounded
(`max_bytes`) so a huge first-sync backlog can't be slurped into memory at once.

If registration reports `remapped: true` (the central store keeps the first-seen
`repository_id` for this checkout — e.g. after a local `.agent-os` re-mint), the
drain rewrites each outgoing event's local `repository_id` to the canonical one
(re-serialized; `event_id` untouched, so idempotency holds) so the events are
queryable under the repository the registry actually knows. The drain also
registers the repo under the **current** machine identity (not the one stamped
into the repo identity file at mint time), so a re-minted machine id can't cause a
permanent register-fail loop.

## Deployment note (M1 → M2/M3) and accepted-risk register

This phase runs the API on Machine 1 bound to loopback (`127.0.0.1:8765`). A LAN
bind for M2/M3 (setting `AGENT_OS_API_HOST=0.0.0.0`) is intentionally deferred.
The **M2 LAN posture is a TLS-terminating reverse proxy** in front of this app
(e.g. Caddy/nginx/an SSH tunnel): the API itself speaks plain HTTP, so
**encryption still lives in that proxy layer** and must land before the bind is
ever flipped. Per-machine auth, previously also deferred to the proxy, now lives
**in the app** (card `8737706e`, Slice A) — see **Auth (two-phase)** above.

Accepted risks for the current single-user / loopback posture (carried forward
from the security review, comment 145 — revisit each before the LAN bind):

1. **Per-machine authorization boundary — now enforced (Slice A).** The API issues
   per-machine `key_id.secret` credentials (hashed at rest) and binds each caller
   to its own `machine_id`, so a per-machine key can no longer register/heartbeat/
   overwrite *another* machine's registry row, and one machine's key rotates or
   revokes without touching the others. The shared `AGENT_OS_API_KEY` remains valid
   as the **admin / bootstrap key** (register machines, issue keys) and — as any
   full-authority admin secret — still bypasses binding, so it must be held only by
   the operator. **Remaining gaps:** (a) transport is still plain HTTP — a bearer
   token (admin or per-machine) is exposed to anyone who can read the link until
   TLS (Slice B) lands; (b) the sync-cursor endpoints (`/v1/sync/<client_id>`) are
   an admin-only **stopgap** pending per-machine ownership (card `52946159`). Do not
   flip the LAN bind, and do not issue a per-machine key to a sync consumer, before
   those land.
2. **Default bind is loopback-only.** At `127.0.0.1:8765` only local processes on
   M1 can reach the API, so everything above (and the batch-size ceiling) is
   scoped to "another local process on the same machine" until the LAN bind. At
   that point re-evaluate the shared-key model and the 500-event / 40 MiB caps.
3. **`GET /health` is intentionally unauthenticated** — it returns only
   `{"status":"ok","service":"agent-os-api"}`, no data and no DB touch.
4. **Batch/body bounds** are enforced server-side now (≤ 500 events → `413`;
   `MAX_CONTENT_LENGTH` 40 MiB), so a bug or leaked key can't submit an unbounded
   single-transaction batch.
5. **Drain child env is allowlisted** (`mcp/sync_server.py`): the spawned drain
   inherits only `AGENT_OS_API_URL`/`_KEY`/`_SYNC_*`/`_HOME`/`_EVENTS_*` plus
   `PATH`/`HOME`/`PYTHON*` — the DB connection strings it never uses are withheld.

## `ops.sync_cursors` (decision: implemented, card `93baf05b`)

Migration `003` created `ops.sync_cursors` (per-consumer replay position); the 1c
drain still tracks its OWN cursor in a **local file**
(`.agent-os/sync/cursor.json`, unaffected by any of this). 1d's `services.m3_client`
is the first SERVER-SIDE puller, and persisting its `--follow` position across
restarts is exactly this table's purpose — so the decision is **implement**, via
`GET/PUT /v1/sync/{client_id}` (`services/api/app.py`, upserting on the table's
PK `client_id`), not drop it. Migration `005` added `last_seq BIGINT NULL`: the
original `last_event_id`-only column (`003`, before `ingest_seq` existed) has no
ordering information of its own and can't drive `GET /v1/events/recent`'s
`?after_seq=` keyset, so `last_seq` is the authoritative resume position and
`last_event_id` is kept alongside purely for human-readable diagnosis. See
`database/README.md` for the full rationale.

## M3 stand-in client (`services/m3_client/`, card `93baf05b`)

`python -m services.m3_client status [--json] [--follow] [--client-id ID]
[--limit N] [--repository-id ID] [--interval SECONDS]` — a small terminal
client playing Machine 3's role (list machines/repositories/active
sessions/recent events) until the real M3 VM and its communication agent exist
(Phase 6 replaces this human-eyeball stand-in with that agent). Proves the
control-plane READ path end to end; see the three-terminal demo above.

**Stdlib-only, end to end — deliberately more restrictive than `services.api` /
`outbox.drain`.** This package imports nothing beyond the Python standard
library: no `contracts`, no `pydantic`, and — unlike `services.api.__main__` and
`outbox.drain`, both of which call `load_dotenv()` for M1 developer convenience
— no `python-dotenv` either. It must eventually run standalone on a bare-Python
M3 VM with none of this repo's other dependencies installed, so configuration
comes only from `--api-url`/`--api-key` or the plain process environment
(`AGENT_OS_API_URL` / `AGENT_OS_API_KEY` — export them yourself, or use `env
$(cat .env | xargs) python -m services.m3_client status` from this repo).

**`--json`** emits the raw combined snapshot (machines/repositories/sessions/
recent events) as one JSON object instead of the plain-text board — the
integration seam a future Telegram/M3-agent consumer would read instead of a
human.

**`--follow`** polls for new events after the initial board and prints each as
it arrives, using a **keyset walk** (`services/m3_client/follow.py`) over the
same `GET /v1/events/recent?after_seq=` pagination the API already offers:
each tick fetches the newest page and, only if an entire page is still newer
than the last-seen `ingest_seq` (i.e. more events arrived than fit in one page),
keeps turning pages backward via `next_after` until it reaches an event at or
before that cursor — so an arbitrarily large burst between polls is caught with
no drops or duplicates, while the common case (a quiet tick, or fewer than one
page of new events) costs exactly one request. The cursor never regresses
below its previous value even if the server ever reports a lower top
`ingest_seq` (e.g. a wiped dev database), which would otherwise re-report an
already-shown burst as new. `--follow` persists this position via
`GET`/`PUT /v1/sync/{client_id}` under `--client-id` (default `m3-standin`), so
restarting the client resumes from where it left off instead of replaying
history or missing the gap in between.
