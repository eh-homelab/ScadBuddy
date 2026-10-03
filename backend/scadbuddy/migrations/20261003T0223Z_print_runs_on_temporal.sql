-- #1052: print runs run as Temporal workflows (spec 2026-10-01 §5). The row is written
-- only by the `PrintRun` workflow's activities, so nothing beats a heartbeat and nothing
-- expires a lost run. `workflow_id`/`workflow_run_id` name the execution that owns the
-- row; the unique pair makes the first activity's insert idempotent under retry (§4.2).
ALTER TABLE print_runs DROP COLUMN heartbeat_at;
ALTER TABLE print_runs ADD COLUMN workflow_id text;
ALTER TABLE print_runs ADD COLUMN workflow_run_id text;
CREATE UNIQUE INDEX print_runs_execution ON print_runs (workflow_id, workflow_run_id);
-- A run a pre-#1052 process left `running` has no workflow to finish it.
UPDATE print_runs SET status = 'failed', finished_at = now(),
    error = jsonb_build_object(
        'type', 'about:blank', 'status', 500, 'title', 'Internal Server Error',
        'extensions', '{}'::jsonb,
        'detail', 'ScadBuddy was upgraded while it was preparing this print, so it cannot tell whether the print was queued.')
  WHERE status = 'running';
