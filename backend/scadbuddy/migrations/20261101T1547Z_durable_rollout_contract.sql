-- #1142: the contract half of the durable rollout's expand-only migrations, now that no
-- API of a build before them can be running (epic #1058).
--
-- #1052 (#1061): print runs are Temporal workflows; only a pre-#1052 pod beat a run's
-- `heartbeat_at`. A row such a pod left running has no execution to end it, and may
-- have queued (the words `bambuddy/runs.py` gave it as `UPGRADE_INTERRUPTED`).
UPDATE print_runs SET status = 'failed', finished_at = now(), enqueue_attempted = true,
    error = jsonb_build_object(
        'type', 'about:blank', 'status', 500, 'title', 'Internal Server Error',
        'extensions', '{}'::jsonb,
        'detail', 'ScadBuddy was upgraded while it was preparing this print, so it cannot tell whether the print was queued; check Bambuddy''s queue before printing again.')
  WHERE status = 'running' AND workflow_id IS NULL;
ALTER TABLE print_runs DROP COLUMN heartbeat_at;

-- #1053 (#1066): renders run as `render-<render_key>`, one row per execution. A row an
-- older build inserted (no `workflow_run_id`) still unsettled has no workflow of this
-- build to settle it, and with the settle pass's legacy half gone nothing else would.
UPDATE render_jobs SET state = 'failed', finished_at = now(),
    error = 'failed: the upgrade left it waiting with no workflow to run it'
  WHERE workflow_run_id IS NULL AND state IN ('pending', 'running');
DROP INDEX render_jobs_pending_key;
-- `inputs` holds the parameters as `inputs.params` on every row (backfilled by
-- `20260928T0900Z_render_jobs_projection.sql`); a row written with none takes them now.
UPDATE render_jobs SET inputs = jsonb_build_object('params', params, 'v', 0)
  WHERE inputs = '{}'::jsonb;
ALTER TABLE render_jobs DROP COLUMN params;

-- #268, #1091: `FollowPrint` follows prints; the old in-process watcher's log was
-- handed to it at each start and nothing writes it.
DROP TABLE print_watches;
