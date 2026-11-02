-- #4975 phase 6 (spec 2026-11-01 §7.4, §7.5; D6).

-- Personal access tokens (§7.5): minted by a signed-in person in Settings for
-- `bambuddy-fs` and the Go client, shown once, kept as their SHA-256 only. The
-- principal is the person who minted it; `scope` is `read` (safe methods) or `write`.
CREATE TABLE personal_access_tokens (
    id           text        PRIMARY KEY,
    principal    text        NOT NULL,
    name         text        NOT NULL,
    scope        text        NOT NULL CHECK (scope IN ('read', 'write')),
    token_hash   bytea       NOT NULL UNIQUE CHECK (octet_length(token_hash) = 32),
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    last_used_at timestamptz,
    revoked_at   timestamptz
);

CREATE INDEX personal_access_tokens_principal ON personal_access_tokens (principal, created_at DESC);

-- A delegated session's one-time sign-in code (§7.4): the agent's credential mints one
-- for the principal of the agent session a browser serves; the browser spends it at
-- `GET /api/v1/auth/delegated?code=` for a `delegated` session of `ttl_seconds`. Kept as
-- its SHA-256; a minute to use, once.
CREATE TABLE auth_delegation_codes (
    code_hash   bytea       PRIMARY KEY CHECK (octet_length(code_hash) = 32),
    principal   text        NOT NULL,
    ttl_seconds integer     NOT NULL CHECK (ttl_seconds BETWEEN 60 AND 3600),
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz
);
