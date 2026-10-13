-- #2484 (spec 2026-10-11-flow-grants-and-git-design.md §12): flow triggers, which start
-- runs of a pinned flow version on a Temporal Schedule or on an event, as their creator.
-- The row is the only record a fire reads: the Schedule's action carries the trigger id
-- only. `created_by`/`updated_by` are written by the routes from the request's author.
CREATE TABLE flow_triggers (
    id                 text PRIMARY KEY,
    name               text NOT NULL CHECK (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    definition_id      text NOT NULL,
    kind               text NOT NULL CHECK (kind IN ('schedule', 'event')),
    -- kind 'schedule': {cron | interval_s, offset_s, time_zone, catchup_window_s}.
    schedule           jsonb,
    -- kind 'event': {kind, slug}.
    event              jsonb,
    overlap            text NOT NULL DEFAULT 'skip' CHECK (overlap IN ('skip', 'allow_all')),
    inputs             jsonb,
    approval_timeout_s integer,
    paused             boolean NOT NULL DEFAULT false,
    paused_at          timestamptz,
    paused_by          jsonb,
    paused_reason      text,
    created_by         jsonb NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_by         jsonb,
    updated_at         timestamptz,
    CHECK ((kind = 'schedule') = (schedule IS NOT NULL)),
    CHECK ((kind = 'event') = (event IS NOT NULL))
);

CREATE INDEX flow_triggers_name ON flow_triggers (name);
CREATE INDEX flow_triggers_event ON flow_triggers ((event ->> 'kind')) WHERE kind = 'event';

-- A trigger's standing grant (§12.4): what its fires' runs may do without asking. A
-- person approves it; at each fire while it is in force, the run gets a run grant
-- (`flow_grants.standing_grant_id`) with the same rules and fresh counters.
CREATE TABLE flow_trigger_grants (
    id           text PRIMARY KEY,
    trigger_id   text NOT NULL REFERENCES flow_triggers (id) ON DELETE CASCADE,
    status       text NOT NULL CHECK (status IN ('requested', 'approved', 'declined')),
    spec         jsonb NOT NULL,
    requested_by jsonb NOT NULL,
    approved_by  text,
    approved_at  timestamptz,
    revoked_by   text,
    revoked_at   timestamptz,
    expires_at   timestamptz,
    -- Per rule index: {prints, filament_g, calls} across every run it seeded: its
    -- limits are spent cumulatively, under its row lock (`FlowStore.consume_grant`).
    used         jsonb NOT NULL DEFAULT '{}',
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- At most one live standing grant per trigger.
CREATE UNIQUE INDEX flow_trigger_grants_live ON flow_trigger_grants (trigger_id)
    WHERE status IN ('requested', 'approved') AND revoked_at IS NULL;

-- One row per fire, under a key a retry keeps, so a retry or an event heard by every
-- replica starts one run. Not a foreign key: a run keeps naming the trigger that started
-- it after the trigger is deleted.
CREATE TABLE flow_trigger_fires (
    trigger_id text NOT NULL,
    fire_key   text NOT NULL,
    run_id     text NOT NULL,
    outcome    text NOT NULL CHECK (outcome IN ('pending', 'admitted', 'started', 'skipped', 'failed')),
    reason     text,
    event      jsonb,
    fired_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (trigger_id, fire_key)
);

CREATE INDEX flow_trigger_fires_recent ON flow_trigger_fires (trigger_id, fired_at DESC);
CREATE INDEX flow_trigger_fires_run ON flow_trigger_fires (run_id);

-- The standing grant a run grant was seeded from: revoking it revokes them.
ALTER TABLE flow_grants ADD COLUMN IF NOT EXISTS standing_grant_id text;
CREATE INDEX IF NOT EXISTS flow_grants_standing ON flow_grants (standing_grant_id)
    WHERE standing_grant_id IS NOT NULL;
