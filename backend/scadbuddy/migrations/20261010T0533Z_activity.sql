-- Activity (spec 2026-10-09 principal jobs, plan deviation 1): one row per long-running
-- thing, written by its source at the place it announces a change. `id` is
-- `<source>:<source id>`; `principal`, `session` and `via_tool` are set once by the
-- route that started it and never overwritten by a status write.
CREATE TABLE activity (
    id text PRIMARY KEY,
    kind text NOT NULL,
    principal text,
    session text,
    via_tool text,
    subject text NOT NULL DEFAULT '',
    status text NOT NULL CHECK (status IN ('queued', 'running', 'waiting', 'succeeded', 'failed', 'cancelled')),
    title text NOT NULL,
    parent_id text,
    facets jsonb NOT NULL DEFAULT '{}'::jsonb,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    source text NOT NULL,
    source_id text NOT NULL,
    workflow_id text,
    run_id text,
    traceparent text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CHECK ((status IN ('succeeded', 'failed', 'cancelled')) = (finished_at IS NOT NULL))
);
CREATE INDEX activity_recent ON activity (created_at DESC, id DESC);
CREATE INDEX activity_principal ON activity (principal, created_at DESC);
CREATE INDEX activity_kind ON activity (kind, created_at DESC);
CREATE INDEX activity_open ON activity (status) WHERE finished_at IS NULL;
CREATE INDEX activity_parent ON activity (parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX activity_facets ON activity USING gin (facets jsonb_path_ops);
