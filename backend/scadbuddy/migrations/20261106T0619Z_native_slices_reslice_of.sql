-- The dialog's Reslice (#4433): a fresh native slice gets a row of its own, so the
-- completed row it replaces stays readable until the new slice completes. `reslice_of`
-- is the `workflow_id` of the slice's first row (`native_workflow_id`), the key a start
-- finds the newest row by. It is NULL on that first row. The column is nullable, so a
-- start that does not know it (an older pod during a rollout) still inserts.
ALTER TABLE native_slices ADD COLUMN reslice_of text;
CREATE INDEX native_slices_reslice_of ON native_slices (reslice_of) WHERE reslice_of IS NOT NULL;
