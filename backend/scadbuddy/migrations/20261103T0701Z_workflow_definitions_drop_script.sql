-- #5139 (#1142): a flow version's script is its commit in `data/flows` (#2503), and the
-- start-time backfill that committed the rows written before that is gone. A row with no
-- commit still holds the only copy of its script, so the column is dropped only when no
-- such row is left; otherwise it stays, with a notice, since a raise here would fail the
-- API's start. `flows/backfill.py`, which `20261012T0707Z_workflow_definition_commits.sql`
-- names, is gone; `flows/reconcile.py` is what remains of it.
DO $$
DECLARE
    left_over bigint;
BEGIN
    SELECT count(*) INTO left_over FROM workflow_definitions WHERE commit IS NULL;
    IF left_over = 0 THEN
        ALTER TABLE workflow_definitions DROP COLUMN script;
    ELSE
        RAISE NOTICE 'workflow_definitions.script kept: % flow version(s) have no commit, and their script is only there', left_over;
    END IF;
END
$$;
