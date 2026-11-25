-- tasks (#7364): a typed question a flow asks a person. One row per question; the id is
-- the harness call id. A run's tasks go with the run when it is forgotten.
-- place becomes part of a realtime topic, so it holds no control character.
CREATE TABLE tasks (
    id          text PRIMARY KEY,
    run_id      text NOT NULL REFERENCES workflow_runs (id) ON DELETE CASCADE,
    title       text NOT NULL CONSTRAINT tasks_title CHECK (length(title) BETWEEN 1 AND 500),
    schema      jsonb NOT NULL,
    context     jsonb NOT NULL DEFAULT '[]',
    place       text CONSTRAINT tasks_place CHECK
                  (length(place) BETWEEN 1 AND 100 AND place !~ '[[:cntrl:]]'),
    ui          text,
    expires_at  timestamptz,
    state       text NOT NULL DEFAULT 'open' CONSTRAINT tasks_state
                  CHECK (state IN ('open', 'answered', 'cancelled', 'expired')),
    answer      jsonb,
    answered_by text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tasks_open_place ON tasks (place, created_at) WHERE state = 'open';
CREATE INDEX tasks_run ON tasks (run_id);
