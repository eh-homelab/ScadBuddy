-- #241: the render queue (scadbuddy.render.pg_store). One table is both the job
-- record and the wait list.
CREATE TABLE render_jobs (
    id            text PRIMARY KEY,
    slug          text NOT NULL,
    params        jsonb NOT NULL DEFAULT '{}'::jsonb,
    model_version text,
    state         text NOT NULL CHECK (state IN ('pending', 'running', 'done', 'failed')),
    created_at    timestamptz NOT NULL,
    started_at    timestamptz,
    finished_at   timestamptz,
    log_tail      jsonb NOT NULL DEFAULT '[]'::jsonb,
    error         text,
    result        jsonb,
    render_key    text NOT NULL,
    claims        integer NOT NULL DEFAULT 1,
    attempts      integer NOT NULL DEFAULT 0,
    heartbeat_at  timestamptz
);
CREATE INDEX render_jobs_pending ON render_jobs (created_at, id) WHERE state = 'pending';
CREATE UNIQUE INDEX render_jobs_pending_key ON render_jobs (render_key)
    WHERE state = 'pending';
CREATE INDEX render_jobs_running ON render_jobs (heartbeat_at) WHERE state = 'running';
CREATE INDEX render_jobs_unfinished_slug ON render_jobs (slug)
    WHERE state IN ('pending', 'running');
CREATE INDEX render_jobs_settled ON render_jobs (finished_at)
    WHERE state IN ('done', 'failed');
