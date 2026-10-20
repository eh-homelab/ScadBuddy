-- #4117: `copied_refs` (store/index.py) asks for a migration's latest copies,
-- `ORDER BY touched_at DESC LIMIT n`, and `store_blobs_backend_migrated` gave it no
-- order, so it sorted every copied row. With `touched_at` last, the index reads them
-- in order; every other query on (backend, migrated_backend) uses it as a prefix.
-- A plain CREATE INDEX (migrate runs every file in its transaction, so not
-- CONCURRENTLY): writes to store_blobs wait while it builds, a moment at the size
-- store_blobs has. A store far larger needs it built CONCURRENTLY by hand first.
CREATE INDEX IF NOT EXISTS store_blobs_backend_migrated_touched
    ON store_blobs (backend, migrated_backend, touched_at DESC);
DROP INDEX IF EXISTS store_blobs_backend_migrated;
