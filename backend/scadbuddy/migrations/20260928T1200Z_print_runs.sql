-- #470: the print dialog's runs (`bambuddy.runs.PrintRunStore`). `POST
-- /print/outputs/{id}/run` answers 202 with a row here and slices and queues in the
-- background; `GET /print/runs/{id}` reads it back on any replica. `idempotency_key`
-- is the output plus the request body, so a retry finds the run it repeats.
-- `heartbeat_at` is touched while the run is alive: a `running` row whose heartbeat
-- stopped was lost to a restart, and reads as failed.
CREATE TABLE print_runs (
    id              text PRIMARY KEY,
    output_id       text NOT NULL,
    idempotency_key text NOT NULL,
    status          text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    result          jsonb,
    error           jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    heartbeat_at    timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);
CREATE INDEX print_runs_key ON print_runs (idempotency_key, created_at DESC);
CREATE INDEX print_runs_finished_at ON print_runs (finished_at);
