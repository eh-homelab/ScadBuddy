-- The activity repair pass (activity/index.py `_missing`) reads each source for rows
-- created or finished in its window, every 5 minutes. The OR is served by a bitmap of
-- one index per side; `finished_at` is already indexed on operations and print_runs.
CREATE INDEX IF NOT EXISTS render_jobs_created_at ON render_jobs (created_at);
CREATE INDEX IF NOT EXISTS render_jobs_finished_at ON render_jobs (finished_at);
CREATE INDEX IF NOT EXISTS print_runs_created_at ON print_runs (created_at);
CREATE INDEX IF NOT EXISTS operations_created_at ON operations (created_at);
