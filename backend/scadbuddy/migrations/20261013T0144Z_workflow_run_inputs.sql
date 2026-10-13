-- #2483: a run's inputs, as its workflow recorded them when it started (validated, its
-- flow's defaults filled in). NULL for a run of a flow with no inputs schema, and for
-- every run started before.
ALTER TABLE workflow_runs ADD COLUMN IF NOT EXISTS inputs jsonb;
