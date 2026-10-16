-- Pipeline outputs on the job (spec 2026-09-27 §5.2): Generate saves each one.
ALTER TABLE render_jobs ADD COLUMN IF NOT EXISTS outputs jsonb NOT NULL DEFAULT '[]'::jsonb;
