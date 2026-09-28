-- #424: render_jobs becomes the projection a Temporal workflow writes in place
-- (spec 2026-09-27 §3.2). Additive: the legacy queue keeps running on the same table
-- until the final phase-1 PR drops heartbeat_at and its index.
ALTER TABLE render_jobs DROP CONSTRAINT render_jobs_state_check;
ALTER TABLE render_jobs ADD CONSTRAINT render_jobs_state_check
    CHECK (state IN ('pending', 'running', 'done', 'failed', 'cancelled'));
ALTER TABLE render_jobs
    ADD COLUMN workflow_id text,
    ADD COLUMN kind text NOT NULL DEFAULT 'render' CHECK (kind IN ('render', 'arrange')),
    ADD COLUMN inputs jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN pipeline_version text NOT NULL DEFAULT 'default',
    ADD COLUMN steps jsonb NOT NULL DEFAULT '[]'::jsonb;
UPDATE render_jobs SET inputs = jsonb_build_object('params', params);
CREATE INDEX render_jobs_stale_pending ON render_jobs (created_at)
    WHERE state = 'pending' AND started_at IS NULL;
CREATE TABLE blob_refs (
    key         text NOT NULL,
    holder_kind text NOT NULL,
    holder_id   text NOT NULL,
    PRIMARY KEY (key, holder_kind, holder_id)
);
CREATE INDEX blob_refs_key ON blob_refs (key);
