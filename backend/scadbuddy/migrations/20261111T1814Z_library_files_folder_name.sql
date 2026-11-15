-- #6049: `NativeLibraryStore.files` with a `filename` (the print watcher's look for a
-- job's file in the inbox folder, #4699 phase 4) filters live rows on `folder_id` and
-- `name`; `library_files_folder` serves only the folder, so Postgres read every live row
-- of it. A plain `CREATE INDEX` takes a `SHARE` lock on `library_files` while it builds
-- (backend/CLAUDE.md, the activity migration's note); the table holds one row per library
-- file, so the build is short.
CREATE INDEX IF NOT EXISTS library_files_folder_name ON library_files (folder_id, name)
    WHERE deleted_at IS NULL;
