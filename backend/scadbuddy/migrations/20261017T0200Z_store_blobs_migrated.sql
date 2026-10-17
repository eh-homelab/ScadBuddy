-- A row's copy on the backend a migration moves it to (spec 2026-10-16 §4.3): written by
-- the copy phase, made the row's own at the next start on that backend.
ALTER TABLE store_blobs ADD COLUMN migrated_backend text, ADD COLUMN migrated_backend_id text;
CREATE INDEX store_blobs_backend_migrated ON store_blobs (backend, migrated_backend);
