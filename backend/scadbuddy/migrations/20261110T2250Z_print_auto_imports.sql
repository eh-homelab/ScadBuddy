-- #4699: ScadBuddy imports the prints it did not start, with no user step
-- (`print_watch/`). One row per Bambuddy archive the watcher took: `linked` when it
-- linked the archive to a model (its `print_links` and `print_imports` rows are written
-- in the same transaction), `inbox` when no model matched with confidence or no source
-- had the 3MF (`reason` says why). `matched_by` is how a model was matched: an earlier
-- import of the same printed file, or the job's name. `suggested` holds the models the
-- job's name suggests, for the inbox. `content_hash` and `started_at` are Bambuddy's for
-- the archive: with the printer they name the job, so an archive Bambuddy makes again for
-- the same job (a re-sync) is never imported twice.
CREATE TABLE IF NOT EXISTS print_auto_imports (
    archive_id      bigint PRIMARY KEY,
    printer_id      bigint,
    print_name      text,
    content_hash    text,
    started_at      timestamptz,
    state           text NOT NULL CHECK (state IN ('linked', 'inbox')),
    slug            text,
    matched_by      text CHECK (matched_by IN ('earlier_import', 'job_name')),
    source          text CHECK (source IN ('archive', 'archive_source', 'library', 'printer')),
    library_file_id bigint,
    printer_path    text,
    filename        text,
    size            bigint,
    sha256          text,
    reason          text,
    suggested       text[] NOT NULL DEFAULT '{}',
    seen_at         timestamptz NOT NULL DEFAULT now(),
    CHECK ((state = 'linked') = (slug IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS print_auto_imports_job
    ON print_auto_imports (printer_id, content_hash, started_at)
    WHERE printer_id IS NOT NULL AND content_hash IS NOT NULL AND started_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS print_auto_imports_state
    ON print_auto_imports (state, archive_id DESC);

-- When the watcher was last switched on: it takes only the archives Bambuddy made since,
-- so switching it on never imports the whole history. One row, written by the first pass
-- that finds the switch on and deleted by a pass that finds it off.
CREATE TABLE IF NOT EXISTS print_auto_import_watch (
    only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row),
    since    timestamptz NOT NULL DEFAULT now()
);

-- The earlier imports of a printed file, by its hash (`AutoImportStore.slugs_by_hash`).
CREATE INDEX IF NOT EXISTS print_imports_sha256 ON print_imports (sha256)
    WHERE sha256 IS NOT NULL;
