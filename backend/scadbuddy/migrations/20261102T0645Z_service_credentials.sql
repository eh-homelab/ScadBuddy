-- #4975 phase 4 (spec 2026-11-01 §7.1, §7.2; D4): the API-issued bearer credentials of
-- the agent, the print worker and the projects worker. Verified by `secret_hash`
-- (SHA-256); `secret_sealed` exists only so the caller can be handed its token, sealed
-- under a per-row data key the KEK seals, its AAD binding `id`, `caller` and `scopes`.
CREATE TABLE service_credentials (
    id            text        PRIMARY KEY CHECK (id ~ '^[0-9a-f]{16}$'),
    caller        text        NOT NULL CHECK (caller IN ('agent', 'print', 'projects')),
    scopes        text[]      NOT NULL,
    secret_hash   bytea       NOT NULL UNIQUE CHECK (octet_length(secret_hash) = 32),
    secret_sealed bytea       NOT NULL,
    dek_sealed    bytea       NOT NULL,
    kek_id        text        NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    revoked_at    timestamptz,
    last_used_at  timestamptz
);

CREATE INDEX service_credentials_caller ON service_credentials (caller, expires_at DESC);

-- One view per caller, which only that caller's own role is granted (§7.2,
-- `auth/service_credentials.py` `grant_caller_views`): its live credentials, sealed.
CREATE VIEW service_credentials_agent AS
    SELECT id, caller, scopes, secret_sealed, dek_sealed, kek_id, expires_at
    FROM service_credentials
    WHERE caller = 'agent' AND revoked_at IS NULL AND expires_at > now();

CREATE VIEW service_credentials_print AS
    SELECT id, caller, scopes, secret_sealed, dek_sealed, kek_id, expires_at
    FROM service_credentials
    WHERE caller = 'print' AND revoked_at IS NULL AND expires_at > now();

CREATE VIEW service_credentials_projects AS
    SELECT id, caller, scopes, secret_sealed, dek_sealed, kek_id, expires_at
    FROM service_credentials
    WHERE caller = 'projects' AND revoked_at IS NULL AND expires_at > now();
