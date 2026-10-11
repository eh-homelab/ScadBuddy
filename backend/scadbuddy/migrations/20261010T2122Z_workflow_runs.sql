-- A flow's runs, written only by ProjectWorkflow's activities (plan 6 Ruling 7): the
-- first activity inserts the row, so a row exists only for a run that started.
-- steps: one entry per host call, never its arguments or result. waiting_on: the
-- calls parked for a person. approval_timeout_s: the run's resolved value (0 never).
CREATE TABLE workflow_runs (
    id                 text PRIMARY KEY,
    definition_id      text NOT NULL REFERENCES workflow_definitions (id),
    version            integer NOT NULL,
    name               text NOT NULL,
    status             text NOT NULL CHECK (status IN
                         ('running', 'waiting', 'succeeded', 'failed', 'terminated')),
    waiting_on         jsonb NOT NULL DEFAULT '[]',
    steps              jsonb NOT NULL DEFAULT '[]',
    result             text,
    result_truncated   boolean NOT NULL DEFAULT false,
    approval_timeout_s integer NOT NULL DEFAULT 0 CHECK (approval_timeout_s >= 0),
    workflow_id        text NOT NULL UNIQUE,
    workflow_run_id    text NOT NULL,
    started_by         jsonb NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX workflow_runs_definition ON workflow_runs (definition_id, created_at DESC);
CREATE INDEX workflow_runs_open ON workflow_runs (updated_at) WHERE status IN ('running', 'waiting');
CREATE INDEX workflow_runs_session ON workflow_runs ((started_by ->> 'session'), created_at DESC);
