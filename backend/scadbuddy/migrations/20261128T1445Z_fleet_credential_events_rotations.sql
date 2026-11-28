-- The fleet's login rotator (#7984; spec 2026-11-26-fleet-session-registry-and-rotator-design
-- §6.2): the credential events a machine reads from its transcripts, and the rotations it makes.
-- Core's `fleet-rotator` routes write both; a machine writes only rows of its own machines.
--
-- credential_id names an ai_credentials row with no foreign key, as fleet_session_hosts does:
-- that table is the agent's schema, applied after this one.

-- One row per transcript entry: entry_uuid is the entry's uuid, so an event a machine sends
-- twice (a restart re-reads a line) is recorded and applied once.
CREATE TABLE fleet_credential_events (
    entry_uuid uuid PRIMARY KEY,
    machine_id text NOT NULL REFERENCES fleet_machines ON DELETE CASCADE,
    session_id uuid,
    credential_id text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('rate_limit', 'permanent', 'transient', 'recovery')),
    reset_at timestamptz,
    reset_source text CHECK (reset_source IN ('epoch', 'text', 'default')),
    reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX fleet_credential_events_credential ON fleet_credential_events (credential_id, created_at DESC);

-- A rotation moves one config dir of a machine from one login to another. sessions is the
-- check (§6.7): one {session_id, result, at} per told session. verified_at is set when one
-- session passes.
CREATE TABLE fleet_rotations (
    id uuid PRIMARY KEY,
    machine_id text NOT NULL REFERENCES fleet_machines ON DELETE CASCADE,
    config_dir text,
    from_credential_id text,
    to_credential_id text,
    reason text NOT NULL,
    rotated_at timestamptz NOT NULL DEFAULT now(),
    sessions jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(sessions) = 'array'),
    verified_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX fleet_rotations_machine ON fleet_rotations (machine_id, rotated_at DESC);
