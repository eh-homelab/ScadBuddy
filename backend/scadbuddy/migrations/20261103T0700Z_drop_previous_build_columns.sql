-- #5139: the second contract step of the durable rollout (#1142, epic #1058), once no
-- pod of the build before #5124 runs. Its pre-#1052 print-run passes selected on
-- `heartbeat_at`, and its render workers read `render_jobs.params`, which this build
-- reads from `inputs.params` (filled for every row by
-- `20261101T1547Z_render_pending_key_and_print_watches.sql`). A flow version's script
-- is `20261103T0701Z_workflow_definitions_drop_script.sql`, a file of its own because
-- the agent's flow tests apply the flow migrations to a database without these tables.
ALTER TABLE print_runs DROP COLUMN heartbeat_at;
ALTER TABLE render_jobs DROP COLUMN params;
