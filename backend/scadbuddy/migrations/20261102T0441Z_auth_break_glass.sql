-- #4975 phase 3 (spec 2026-11-01 §9.3): break-glass sign-in codes, minted by
-- `python -m scadbuddy.auth.break_glass` in the pod. Kept as their SHA-256 only; a code
-- signs in once (`used_at`), within ten minutes.
CREATE TABLE auth_break_glass_codes (
    code_hash  bytea       PRIMARY KEY CHECK (octet_length(code_hash) = 32),
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    used_at    timestamptz
);
