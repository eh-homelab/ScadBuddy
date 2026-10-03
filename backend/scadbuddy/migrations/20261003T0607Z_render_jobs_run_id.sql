-- Renders on the command shape (#1053, spec 2026-10-01 §4.5): the workflow
-- `render-<render_key>` inserts its row in its first activity, once per execution, and
-- coalesces identical requests in its own state. A row is unique per execution; the
-- pending row is no longer unique per render key (a legacy row may still wait beside a
-- new one while the old build drains).
ALTER TABLE render_jobs ADD COLUMN workflow_run_id text;
CREATE UNIQUE INDEX render_jobs_execution ON render_jobs (workflow_id, workflow_run_id);
DROP INDEX render_jobs_pending_key;
