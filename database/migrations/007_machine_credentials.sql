-- Migration 007: registry.machine_credentials — per-machine API keys for the
-- central /v1 API (card 8737706e, Slice A: auth code; TLS is Slice B). Additive,
-- backward-compatible. The shared AGENT_OS_API_KEY remains valid as the ADMIN /
-- bootstrap key; this table adds O(1) per-machine credentials that the auth layer
-- (services/api/app.py) resolves by key_id and verifies constant-time, so one
-- machine's key can be rotated or revoked without disturbing the others.
--
-- RENUMBERING NOTE: the central-memory epic's schema migration (previously slated
-- as 007) is renumbered to 008 — this credential migration takes 007.
--
-- KEY MODEL: a presented bearer token is '<key_id>.<secret>'. Only SHA-256(secret)
-- (64-hex-char digest) is ever stored here — NEVER the plaintext secret. secrets
-- are high-entropy random (secrets.token_urlsafe(32)), so a fast unsalted hash is
-- appropriate (there is nothing to brute-force the way a low-entropy password
-- would be). key_id is a non-secret identifier ('key_' + ULID), so an O(1) lookup
-- by it leaks nothing; the secret is the part compared in constant time.
--
-- NO uniqueness beyond the key_id PK: a machine may hold SEVERAL active
-- credentials at once (rotation overlap — issue the new key, migrate the machine's
-- .env, then revoke the old). Revocation is a soft delete (revoked_at set) so a
-- revoked key_id can never be re-minted and its audit trail (created_at /
-- last_used_at) survives.
--
-- ID sizing: key_id / machine_id are NVARCHAR(64) uniformly, matching the
-- documented prefixed-ULID convention (database/migrate.py module docstring).

CREATE TABLE registry.machine_credentials (
    -- Non-secret credential identifier ('key_' + ULID). O(1) auth lookup key.
    key_id          NVARCHAR(64)    NOT NULL,
    -- The machine this credential authenticates as. FK to registry.machines so a
    -- credential cannot be issued for an unknown machine (the issuance endpoint
    -- also checks existence first for a clean 404).
    machine_id      NVARCHAR(64)    NOT NULL,
    -- Hex SHA-256 of the secret half of the key (64 chars). NEVER the plaintext.
    -- Sized NVARCHAR(128) for headroom over the 64-char digest.
    secret_hash     NVARCHAR(128)   NOT NULL,
    -- Optional human label (e.g. "m3-drain", "laptop") for operator listings.
    label           NVARCHAR(200)   NULL,
    created_at      DATETIMEOFFSET  NOT NULL
        CONSTRAINT DF_registry_machine_credentials_created_at DEFAULT (SYSDATETIMEOFFSET()),
    -- Best-effort telemetry: refreshed (non-transactionally) on a successful auth.
    last_used_at    DATETIMEOFFSET  NULL,
    -- Soft-delete tombstone: non-NULL means revoked; auth rejects such a key.
    revoked_at      DATETIMEOFFSET  NULL,
    CONSTRAINT PK_registry_machine_credentials PRIMARY KEY (key_id),
    CONSTRAINT FK_registry_machine_credentials_machine FOREIGN KEY (machine_id)
        REFERENCES registry.machines (machine_id)
);

-- List/rotate a single machine's credentials (GET /v1/machines/<id>/credentials).
CREATE INDEX IX_registry_machine_credentials_machine_id
    ON registry.machine_credentials (machine_id);
