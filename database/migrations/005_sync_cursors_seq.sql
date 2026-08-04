-- Migration 005: ops.sync_cursors gets a numeric replay position (card 93baf05b,
-- Phase 1d "sync-cursor decision"). Additive, backward-compatible.
--
-- ops.sync_cursors was created in migration 003 with only
-- (client_id, last_event_id, updated_at) — before migration 004 added
-- ops.events.ingest_seq, the total-order column GET /v1/events/recent actually
-- pages on via ?after_seq=/next_after. last_event_id (a ULID string) carries no
-- ordering information on its own and cannot drive that keyset, so a client
-- persisting only last_event_id would have no way to resume a --follow walk at
-- the right point. last_seq is the authoritative resume position (the value to
-- send back as ?after_seq= is derived from it, see services/api/store.py);
-- last_event_id is kept for human-readable diagnosis (the event id that seq
-- belongs to) and updated in lockstep, never used to compute the resume point.
ALTER TABLE ops.sync_cursors
    ADD last_seq BIGINT NULL;
