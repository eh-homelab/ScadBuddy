-- #4699 phase 4: the print watcher's record of a print on a LAN-only printer (a registry
-- printer with no Bambuddy link). Bambuddy makes no archive for such a print, so this is
-- ScadBuddy's own print record, keyed by `id`. The printer's LAN watch sees the job
-- start (`printers/reports.py` `started_print`); the print worker reads the job's 3MF
-- over the printer's FTPS while it prints, keeps it in ScadBuddy's own library, and posts
-- the record to the API's CRUD route (`print_watch/lan.py`). A job is the printer, its
-- `subtask_name` and its start (`started_at`, the report that showed it start): the
-- unique index takes each job once, whichever attempt or replica records it. The index
-- is on `printer_key`, the registry id the job was recorded for, which is never null: a
-- resend for a printer deleted before or between sends still finds the job (a unique
-- index treats nulls as distinct, so `printer` alone would not).
-- `state`, `slug`, `matched_by`, `source`, `reason` and `suggested` mean what they mean
-- in `print_auto_imports`; `library_file_id` is a file of ScadBuddy's own library (not
-- Bambuddy's), and `source` is `printer` or null. `matched_by` `import` is a person's
-- link. A printer's delete keeps its prints (`ON DELETE SET NULL`, as `print_runs`).
-- Only the API (the CRUD service) reads or writes it (`print_watch/lan_store.py`).
CREATE TABLE IF NOT EXISTS print_lan_imports (
    id              bigserial PRIMARY KEY,
    printer         uuid REFERENCES printers (id) ON DELETE SET NULL,
    printer_key     text NOT NULL,
    subtask_name    text NOT NULL,
    started_at      timestamptz NOT NULL,
    state           text NOT NULL CHECK (state IN ('linked', 'inbox', 'dismissed')),
    slug            text,
    matched_by      text CHECK (matched_by IN ('earlier_import', 'job_name', 'import')),
    source          text CHECK (source IN ('printer')),
    library_file_id bigint,
    printer_path    text,
    filename        text,
    size            bigint,
    sha256          text,
    reason          text,
    suggested       text[] NOT NULL DEFAULT '{}',
    seen_at         timestamptz NOT NULL DEFAULT now(),
    dismissed_at    timestamptz,
    CHECK ((state = 'linked') = (slug IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS print_lan_imports_job
    ON print_lan_imports (printer_key, subtask_name, started_at);
CREATE INDEX IF NOT EXISTS print_lan_imports_state ON print_lan_imports (state, id DESC);
CREATE INDEX IF NOT EXISTS print_lan_imports_sha256 ON print_lan_imports (sha256)
    WHERE sha256 IS NOT NULL;
