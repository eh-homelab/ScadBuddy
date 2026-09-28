-- #252: what OpenSCAD reported, parsed. On the row rather than only inside
-- `result`, because a failed render has no result and is when they matter most.
ALTER TABLE render_jobs
    ADD COLUMN diagnostics jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN diagnostics_dropped integer NOT NULL DEFAULT 0;
CREATE INDEX render_jobs_settled_slug ON render_jobs (slug, finished_at DESC)
    WHERE state IN ('done', 'failed');
