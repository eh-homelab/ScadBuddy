-- The client's Idempotency-Key, so a keyed upload over a taken slug is told apart from
-- a re-send of the create that took it before its parts are read (review #1126 1.3).
ALTER TABLE operations ADD COLUMN idempotency_key text;
CREATE INDEX operations_idempotency_key ON operations (idempotency_key)
    WHERE idempotency_key IS NOT NULL;
-- `named_by_running` reads only running rows (review #1126 2.1): main's
-- 20261005T1123Z_operations_running_index.sql indexes them as `operations_running`.
