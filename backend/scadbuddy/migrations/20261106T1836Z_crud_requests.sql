-- #5298 (plan 2026-11-06 D5): the reply of each non-natural CRUD write, keyed by the
-- caller's Idempotency-Key, so a retry after a lost answer gets the first answer back
-- and never writes twice. A row is written in the same transaction as its write
-- (`crud/idempotency.py`) and kept 24 h (pruned by the auth sweep).
CREATE TABLE crud_requests (
    caller      text NOT NULL,
    key         text NOT NULL,
    route       text NOT NULL,
    body_sha256 text NOT NULL,
    status      int NOT NULL,
    body        jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (caller, key)
);
CREATE INDEX crud_requests_created_at ON crud_requests (created_at);
