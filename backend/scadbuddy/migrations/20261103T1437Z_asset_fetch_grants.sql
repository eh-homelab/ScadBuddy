-- #959: the nonces of the one-off host grants spent (`library/fetch_grants.py`), so a
-- grant serves one fetch. A re-send of that fetch under its own Idempotency-Key is the
-- same fetch, not a second one. A row is kept until its grant has expired.
CREATE TABLE asset_fetch_grants (
    nonce           text PRIMARY KEY CHECK (nonce ~ '^[0-9a-f]{32}$'),
    idempotency_key text,
    expires_at      timestamptz NOT NULL
);
