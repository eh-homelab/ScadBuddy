-- #2169: the print dialog's background slices. A run whose copy and presets match a
-- finished preview queues that slice rather than slicing again, so the slice shown is
-- the one printed.
CREATE TABLE IF NOT EXISTS print_preview_slices (
    job_id integer PRIMARY KEY,
    library_file_id integer NOT NULL,
    preset_key text NOT NULL,
    plate_id integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS print_preview_slices_by_key
    ON print_preview_slices (library_file_id, preset_key, created_at DESC);
