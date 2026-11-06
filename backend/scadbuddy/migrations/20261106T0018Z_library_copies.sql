-- Copies between Bambuddy's library and ScadBuddy's own (#5355): one row per copy
-- job (its id is the workflow's), and one per source file it walked, with how it ended.
-- A rerun of the same request keeps the id and rewrites its file rows.
CREATE TABLE library_copies (
    id text PRIMARY KEY,
    direction text NOT NULL,
    request jsonb NOT NULL,
    status text NOT NULL,
    error text,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE TABLE library_copy_files (
    copy_id text NOT NULL REFERENCES library_copies (id) ON DELETE CASCADE,
    source_id bigint NOT NULL,
    -- The folders between the selected root and the file, joined by '/'; '' at the root.
    path text NOT NULL,
    name text NOT NULL,
    size bigint,
    status text NOT NULL,
    reason text,
    target_id bigint,
    target_folder_id bigint,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (copy_id, source_id)
);
CREATE INDEX library_copy_files_pending ON library_copy_files (copy_id) WHERE status = 'pending';
