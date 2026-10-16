-- When each hold was taken (#1007). The output-hold reaper releases an `output` hold
-- whose output has no meta.json, but a save holds its Parts before it writes that
-- file, so only a hold older than the reaper's grace counts as orphaned. Existing
-- rows take the migration's time, so they become eligible only after the grace.
ALTER TABLE blob_refs ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
