-- #2169: the print dialog's background slices. A run whose copy and presets match a
-- finished preview queues that slice rather than slicing again, so the slice shown is
-- the one printed.
--
-- Bambuddy's slice jobs live in its memory and are numbered from 1 again after it
-- restarts, so a job id alone does not name a slice: each row keeps what Bambuddy said
-- about the job when it started (its source file and creation time) and what it sliced
-- to, and a job that no longer says the same is never reused (preview.py).
CREATE TABLE IF NOT EXISTS print_preview_slices (
    id bigserial PRIMARY KEY,
    job_id integer NOT NULL,
    -- The run subject the dialog sliced for: an output id, or library:<file id>.
    subject text NOT NULL,
    library_file_id integer NOT NULL,
    preset_key text NOT NULL,
    plate_id integer NOT NULL,
    -- The job's created_at as Bambuddy reported it; NULL when it did not.
    job_created text,
    -- The sliced file, once the job was seen completed.
    sliced_file_id integer,
    sliced_name text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS print_preview_slices_by_key
    ON print_preview_slices (library_file_id, preset_key, created_at DESC);

CREATE INDEX IF NOT EXISTS print_preview_slices_by_job
    ON print_preview_slices (job_id, subject, created_at DESC);
