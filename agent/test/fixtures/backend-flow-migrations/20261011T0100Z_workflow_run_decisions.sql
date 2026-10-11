-- A person's answer to a flow run's parked call, recorded before the harness is told
-- (plan 2026-10-09-durable-phase-6-flows.md, decisions A and B). request_id is
-- flow:<run id>:<workflow run id>:<call id> (spec 2026-10-01 §6.6), so a decision for a
-- call from before a Reset never matches the run's current one.
CREATE TABLE workflow_run_decisions (
    request_id      text PRIMARY KEY CHECK (request_id ~ '^flow:'),
    run_id          text NOT NULL REFERENCES workflow_runs (id) ON DELETE CASCADE,
    workflow_run_id text NOT NULL,
    call_id         text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('approval', 'answer')),
    outcome         text NOT NULL CHECK (outcome IN ('approved', 'denied', 'answered')),
    response        jsonb NOT NULL DEFAULT '{}',
    responder       text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
