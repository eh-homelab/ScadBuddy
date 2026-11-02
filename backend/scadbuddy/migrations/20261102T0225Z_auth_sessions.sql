-- #4975 phase 1 (spec 2026-11-01 §5.2): a signed-in browser's session. The cookie is
-- never stored, only its SHA-256 (`id_hash`), as MCP bearer tokens are kept hashed.
-- `principal` is `user:<issuer>#<sub>` (`auth/models.py` `principal_for`). A session
-- is live while `revoked_at` is null and now is before both expiries; the idle one
-- slides on use, never past the absolute one (`auth/store.py`).
CREATE TABLE auth_sessions (
    id_hash             bytea       PRIMARY KEY CHECK (octet_length(id_hash) = 32),
    principal           text        NOT NULL,
    issuer              text        NOT NULL,
    subject             text        NOT NULL,
    name                text,
    email               text,
    kind                text        NOT NULL
        CHECK (kind IN ('login', 'delegated', 'break_glass')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    idle_expires_at     timestamptz NOT NULL,
    absolute_expires_at timestamptz NOT NULL,
    revoked_at          timestamptz,
    -- For the audit only (`core/proxies.py` `client_address`), never a check.
    client_address      text,
    user_agent          text,
    CHECK (idle_expires_at <= absolute_expires_at)
);

-- A person's own list and "sign out everywhere".
CREATE INDEX auth_sessions_principal ON auth_sessions (principal);
