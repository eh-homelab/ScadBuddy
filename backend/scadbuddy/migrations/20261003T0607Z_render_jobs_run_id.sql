-- Renders on the command shape (#1053, spec 2026-10-01 §4.5): the workflow
-- `render-<render_key>` inserts its row in its first activity, once per execution, and
-- coalesces identical requests in its own state. A row is unique per execution.
-- `render_jobs_pending_key` stays (expand/contract): the previous build's insert names
-- it as its ON CONFLICT target, and that build serves beside this one through a rolling
-- deploy. A later migration drops it once no pre-#1053 API can be running; until then
-- `JobProjection.accept` keeps one pending row per render key.
ALTER TABLE render_jobs ADD COLUMN workflow_run_id text;
CREATE UNIQUE INDEX render_jobs_execution ON render_jobs (workflow_id, workflow_run_id);
