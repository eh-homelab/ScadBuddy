-- #4975 phase 2 (spec 2026-11-01 §9.2; D1, D3, D7): the login's configuration, one row.
-- `config` is `auth/config.py` `LoginConfig` (nothing secret); the client secret is an
-- envelope under the KEK (`core/secrets.py`), its AAD binding the issuer and client id
-- it was saved for, so it is never sent to another issuer.
CREATE TABLE auth_login_config (
    id            boolean     PRIMARY KEY DEFAULT true CHECK (id),
    config        jsonb       NOT NULL,
    secret_sealed bytea,
    dek_sealed    bytea,
    kek_id        text,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CHECK ((secret_sealed IS NULL) = (dek_sealed IS NULL)),
    CHECK ((secret_sealed IS NULL) = (kek_id IS NULL))
);
