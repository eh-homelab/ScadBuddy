-- Renders on the command shape (#1053, spec 2026-10-01 §4.5): the workflow
-- `render-<render_key>` inserts its row in its first activity, once per execution, and
-- coalesces identical requests in its own state. A row is unique per execution.
-- `render_jobs_pending_key` stays (expand/contract) while rows the previous build
-- inserted may still be pending, their workflows draining on that build's workers:
-- `JobProjection.accept` keeps one pending row per render key until a later migration
-- drops it. The previous build's API must not serve beside this one (README,
-- "Deploying"): its reconciler and its insert would act on this build's rows.
ALTER TABLE render_jobs ADD COLUMN workflow_run_id text;
CREATE UNIQUE INDEX render_jobs_execution ON render_jobs (workflow_id, workflow_run_id);
