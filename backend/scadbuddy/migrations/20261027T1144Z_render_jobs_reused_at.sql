-- A submit answered with a finished job of the same render key (#4870) sets
-- `reused_at`: the prune keeps a row, and with it the `blob_refs` that hold its result,
-- for `job_ttl` past the later of its finish and its last reuse. `finished_at` stays the
-- render's own. The index is the submit's lookup of the newest done row of a key.
ALTER TABLE render_jobs ADD COLUMN reused_at timestamptz;
CREATE INDEX render_jobs_done_key ON render_jobs (render_key, finished_at DESC)
    WHERE state = 'done';
