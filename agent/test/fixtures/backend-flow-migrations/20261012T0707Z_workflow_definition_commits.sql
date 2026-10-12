-- Flow scripts in git (#2503, spec 2026-10-11-flow-grants-and-git-design.md §6.2):
-- `workflow_definitions` keeps one row per version, and each names the commit of
-- `data/flows/<name>/flow.py` that is its script. `commit` is set on every new row;
-- the backend's start backfills the rows written before this (flows/backfill.py).
-- `script` is no longer written or read once that has run; a later file drops it.
ALTER TABLE workflow_definitions ADD COLUMN commit text;
ALTER TABLE workflow_definitions ALTER COLUMN script DROP NOT NULL;
-- The commit a run's script was read from, set by the run's first activity.
ALTER TABLE workflow_runs ADD COLUMN commit text;
