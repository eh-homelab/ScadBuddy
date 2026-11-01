-- #1142: a flow version's script is read from git at its commit (#2503,
-- `20261012T0707Z_workflow_definition_commits.sql`), and the backend's start backfills
-- every row with none (`flows/backfill.py`, which stays for them). A row still without a
-- commit holds the only copy of its script, so the column stays where one exists: the
-- notice says so, the start's backfill commits those rows, and a later file can drop the
-- column once none is left. Never raised: migrations run at the API's start.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM workflow_definitions WHERE commit IS NULL) THEN
        RAISE NOTICE 'workflow_definitions.script kept: a row has no commit yet (#1142)';
    ELSE
        ALTER TABLE workflow_definitions DROP COLUMN script;
    END IF;
END
$$;
