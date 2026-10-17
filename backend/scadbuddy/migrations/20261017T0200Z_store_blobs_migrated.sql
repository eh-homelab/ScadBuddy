-- A row's copy on the backend a migration moves it to (spec 2026-10-16 §4.3): written by
-- the copy phase, made the row's own at the next start on that backend. A copy that
-- failed says why, and when: a later run retries it.
ALTER TABLE store_blobs
    ADD COLUMN migrated_backend text,
    ADD COLUMN migrated_backend_id text,
    ADD COLUMN migrated_error text,
    ADD COLUMN migrated_error_at timestamptz;
CREATE INDEX store_blobs_backend_migrated ON store_blobs (backend, migrated_backend);
