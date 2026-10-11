-- Flows (spec 2026-10-01 §7.3, plan 2026-10-09-durable-phase-6-flows.md Task B1): an
-- agent-written script, registered by name. Versions are immutable: a change is a new
-- version of the name, and each run names the version it ran.
-- approval_timeout_s: how long an outward host call of this flow's runs waits for a
-- decision before it is denied. NULL takes the global setting
-- (flow_approval_timeout_seconds), 0 is never; a run may override either.
CREATE TABLE workflow_definitions (
    id                 text PRIMARY KEY,
    name               text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    version            integer NOT NULL CHECK (version >= 1),
    script             text NOT NULL CHECK (octet_length(script) <= 65536),
    approval_timeout_s integer CHECK (approval_timeout_s >= 0),
    created_by         jsonb NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);
