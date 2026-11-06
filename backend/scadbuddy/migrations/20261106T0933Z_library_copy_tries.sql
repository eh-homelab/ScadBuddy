-- Library copy follow-ups (#5424). `run_id` is the workflow run that the job's start
-- began, so the status route matches the execution by its id and never compares
-- Temporal's clock with Postgres'. `attempts` counts the tries begun on a file, so each
-- file has its own limit. `reason_try` is the try whose failure a pending file's
-- `reason` names. `sha256` is the hash of the bytes a copied or skipped file
-- holds at the target, so a copy finds what another copy put there after its own
-- listing was read.
ALTER TABLE library_copies ADD COLUMN run_id text;
ALTER TABLE library_copy_files
    ADD COLUMN attempts integer NOT NULL DEFAULT 0,
    ADD COLUMN reason_try integer,
    ADD COLUMN sha256 text;
CREATE INDEX library_copy_files_sha256 ON library_copy_files (sha256) WHERE sha256 IS NOT NULL;
