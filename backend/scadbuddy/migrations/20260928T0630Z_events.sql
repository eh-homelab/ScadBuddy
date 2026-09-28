-- #374: the event log (spec §7; `scadbuddy.core.pg_events`). Append-only; `seq`
-- is what a client resumes from (Last-Event-ID), `logged_at` what age pruning reads.
CREATE TABLE events (
    seq        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id   text NOT NULL UNIQUE,
    kind       text NOT NULL,
    at         timestamptz NOT NULL,
    logged_at  timestamptz NOT NULL DEFAULT now(),
    payload    jsonb NOT NULL
);
CREATE INDEX events_logged_at ON events (logged_at);
