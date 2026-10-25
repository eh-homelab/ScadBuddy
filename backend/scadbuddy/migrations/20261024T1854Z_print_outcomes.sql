-- #2665: how each print ended, recorded once. A print is the run that queued or started
-- it (`print_runs`, status 'succeeded'); its follow writes this row the first time a
-- read finds it settled, and publishes `print.outcome` in the same transaction, so two
-- replicas, or two attempts of one follow, record and announce it once. Not a foreign
-- key: a run's row is pruned by its retention, the outcome is kept.
CREATE TABLE print_outcomes (
    run_id     text PRIMARY KEY,
    -- The run subject: an output's id, or `library:<file id>`.
    subject    text NOT NULL,
    slug       text NOT NULL,
    -- The registry printer the run printed on, when it named one.
    printer    uuid,
    outcome    text NOT NULL CHECK (outcome IN ('finished', 'failed', 'cancelled', 'unknown')),
    settled_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX print_outcomes_subject ON print_outcomes (subject, settled_at DESC);
