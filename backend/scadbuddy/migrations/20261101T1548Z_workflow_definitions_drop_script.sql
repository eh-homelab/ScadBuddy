-- #1142: a flow version's script is read from git at its commit (#2503,
-- `20261012T0707Z_workflow_definition_commits.sql`), and the start-time backfill gave
-- every older row one. A row still without a commit would lose its only copy of its
-- script, so stop here: start the previous build once, which backfills it, then this one.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM workflow_definitions WHERE commit IS NULL) THEN
        RAISE EXCEPTION 'a workflow_definitions row has no commit: start the previous'
            ' build once so its backfill commits every flow script to git (#1142)';
    END IF;
END
$$;
ALTER TABLE workflow_definitions DROP COLUMN script;
