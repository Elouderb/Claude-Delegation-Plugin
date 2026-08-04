-- Migration 004: recent-events keyset pagination + session end-time (card 8f84948b
-- review pass). Two additive, backward-compatible changes:
--
-- 1. ops.events.ingest_seq — a monotonic BIGINT IDENTITY. The read endpoint
--    /v1/events/recent previously ordered by occurred_at DESC, which has no
--    unique tiebreaker, so keyset pagination was impossible and a 1d poller had
--    to re-download the same window (and could DROP bursts larger than its
--    limit). ingest_seq is server-assigned in strict central-arrival order and
--    unique, giving a stable total order to page over: ORDER BY ingest_seq DESC
--    with an `after_seq` keyset cursor and a `next_after` continuation token.
--    An IDENTITY column added to a populated table assigns values to the
--    existing rows, so history stays fully ordered.
--
-- 2. registry.agent_sessions.ended_at — when a session finished. The API now
--    materializes/closes session rows from agent.started / agent.finished
--    events (so §17 "list active agents" is answerable from the central read
--    model, not just from the drain's own self-registration); agent.finished
--    stamps ended_at. NULL for a still-running or never-closed session.

ALTER TABLE ops.events
    ADD ingest_seq BIGINT IDENTITY(1,1) NOT NULL;
GO

-- Unique + descending: the covering order for "newest first" reads and the
-- keyset cursor (WHERE ingest_seq < @after_seq ORDER BY ingest_seq DESC).
CREATE UNIQUE INDEX IX_ops_events_ingest_seq
    ON ops.events (ingest_seq DESC);

-- occurred_at had only composite (col, occurred_at) indexes; an occurred_at-
-- leading index supports date-range scans over the append-only log.
CREATE INDEX IX_ops_events_occurred
    ON ops.events (occurred_at DESC);

ALTER TABLE registry.agent_sessions
    ADD ended_at DATETIMEOFFSET NULL;
