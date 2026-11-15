-- #6023: when the watcher's import of an archive last raised (a printer read or upload
-- that may work later, or another Bambuddy error). A pass takes the archives with no row
-- here first, then the failed ones, the oldest failure first, so an archive that fails
-- every pass (a printer down for hours) never holds one of a pass's slots ahead of newer
-- archives. The row is deleted when the archive's `print_auto_imports` row is written,
-- and forgotten once it is older than the pass's look-back (`sweep.LOOKBACK`), as its
-- archive is then never read again. Only the API (the CRUD service) reads or writes it
-- (`print_watch/store.py`).
CREATE TABLE IF NOT EXISTS print_auto_import_failures (
    archive_id bigint PRIMARY KEY,
    failed_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS print_auto_import_failures_failed_at
    ON print_auto_import_failures (failed_at);
