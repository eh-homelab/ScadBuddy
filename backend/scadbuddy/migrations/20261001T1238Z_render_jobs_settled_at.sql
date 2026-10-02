-- #606: prune deletes settled rows by `coalesce(finished_at, created_at)` over done,
-- failed and cancelled; `render_jobs_settled` (finished_at, done/failed only) could
-- serve neither, and no other query reads it.
CREATE INDEX IF NOT EXISTS render_jobs_settled_at
    ON render_jobs ((coalesce(finished_at, created_at)))
    WHERE state IN ('done', 'failed', 'cancelled');
DROP INDEX IF EXISTS render_jobs_settled;
