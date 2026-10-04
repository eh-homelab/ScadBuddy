-- The generic command record (#1053, spec 2026-10-01 §4.2 "Our record"): one row per
-- execution of an `Operation` workflow, written only by its activities.
CREATE TABLE operations (
    id text PRIMARY KEY,
    kind text NOT NULL,
    subject text NOT NULL,
    operation_key text NOT NULL,
    status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    request jsonb NOT NULL,
    result jsonb,
    error jsonb,
    workflow_id text NOT NULL,
    workflow_run_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);
CREATE UNIQUE INDEX operations_execution ON operations (workflow_id, workflow_run_id);
CREATE INDEX operations_operation_key ON operations (operation_key, created_at DESC);
CREATE INDEX operations_finished ON operations (finished_at) WHERE finished_at IS NOT NULL;
