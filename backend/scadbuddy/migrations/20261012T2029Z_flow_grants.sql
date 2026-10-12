-- Run grants (#2481, spec 2026-10-11-flow-grants-and-git-design.md §3): what a person
-- approved for one flow run, and every call it let through. One grant per run, its spec
-- fixed once approved, revocation final (Ruling 6). run_id is not a foreign key: a grant
-- is inserted before the run's first activity writes its workflow_runs row; forgetting
-- the run deletes both, and the flow sweep deletes a grant whose run never started.
CREATE TABLE flow_grants (
    id           text PRIMARY KEY,
    run_id       text NOT NULL UNIQUE,
    status       text NOT NULL CHECK (status IN ('requested', 'approved', 'declined')),
    spec         jsonb NOT NULL,
    -- Per rule index (a narrowing's as '<flow call id>/<index>'): {prints, filament_g, calls}.
    used         jsonb NOT NULL DEFAULT '{}',
    requested_by jsonb NOT NULL,
    approved_by  text,
    approved_at  timestamptz,
    revoked_by   text,
    revoked_at   timestamptz,
    -- The approved spec's expires_at; NULL until approved.
    expires_at   timestamptz,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE flow_grant_uses (
    request_id  text PRIMARY KEY,
    grant_id    text NOT NULL REFERENCES flow_grants (id) ON DELETE CASCADE,
    run_id      text NOT NULL,
    fn          text NOT NULL,
    tool        text,
    input_hash  text NOT NULL,
    rule_index  integer NOT NULL,
    prints      integer NOT NULL DEFAULT 0,
    filament_g  double precision NOT NULL DEFAULT 0,
    session_id  text,
    narrowed_by text,
    -- The defaults the check pinned into the call, as JSON pointers (/request/printer_id).
    pins        jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX flow_grant_uses_run ON flow_grant_uses (run_id, created_at);
-- The cascade from flow_grants finds a grant's uses by it.
CREATE INDEX flow_grant_uses_grant ON flow_grant_uses (grant_id);
