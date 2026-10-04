-- The client's Idempotency-Key, so a keyed upload over a taken slug is told apart from
-- a re-send of the create that took it before its parts are read (review #1126 1.3).
ALTER TABLE operations ADD COLUMN idempotency_key text;
CREATE INDEX operations_idempotency_key ON operations (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
-- `named_by_running` reads only running rows, on every final answer that held a claim
-- (review #1126 2.1).
CREATE INDEX operations_running ON operations (status) WHERE status = 'running';
