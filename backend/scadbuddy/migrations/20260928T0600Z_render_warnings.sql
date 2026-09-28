-- #408: ScadBuddy's own job warnings, on the row for the same reason: a failed
-- render has no result to carry them.
ALTER TABLE render_jobs ADD COLUMN warnings jsonb NOT NULL DEFAULT '[]'::jsonb;
