-- The fleet's session registry (spec 2026-11-26-fleet-session-registry-and-rotator-design
-- §6.3, #7985): the machines that run Claude Code sessions, and where each session runs.
-- The machine service writes it through core's `fleet-rotator` routes; the fleet chat server
-- reads it through core's `fleet` routes.

-- id is the SHA-256, in hex, of the machine's /etc/machine-id and the OS user. The raw
-- machine id is never stored. proof_name is the <machine> of the bootstrap proof file
-- `fleet-rotator.<machine>` whose credential registered the row first: only a credential
-- of that proof refreshes the row or writes its sessions.
CREATE TABLE fleet_machines (
    id text PRIMARY KEY CHECK (id ~ '^[0-9a-f]{64}$'),
    proof_name text NOT NULL CHECK (proof_name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    hostname text NOT NULL,
    os text NOT NULL,
    os_user text NOT NULL,
    service_version text NOT NULL,
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- One row per session id and machine: one session id can run on several machines at once
-- (measured 2026-11-26 in fleet chat: one session id posted from two machines), and each
-- machine writes only its own row. After /clear, Claude Code gives the same process
-- (machine, pid and proc_start) a new session id: that session gets a new row, and
-- previous_session_id links it to the row of the same machine before it.
--
-- credential_id names an ai_credentials row, with no foreign key: that table is the agent's
-- schema (migrations_agent/), which the API applies after this one, so on a new database it
-- does not exist yet here. A deleted credential must not be held back by a session row
-- either. The reads join it by id, and a missing row reads as no credential.
CREATE TABLE fleet_session_hosts (
    session_id uuid NOT NULL REFERENCES fleet_agents ON DELETE CASCADE,
    machine_id text NOT NULL REFERENCES fleet_machines ON DELETE CASCADE,
    pid integer NOT NULL CHECK (pid > 0),
    proc_start text NOT NULL CHECK (proc_start ~ '^[0-9]{1,20}$'),
    cwd text NOT NULL,
    config_dir text NOT NULL,
    transcript_path text,
    git_remote text,
    git_branch text,
    worktree_root text,
    git_head text CHECK (git_head ~ '^[0-9a-f]{40}([0-9a-f]{24})?$'),
    cc_version text,
    kind text,
    entrypoint text,
    name text,
    status text,
    credential_id text,
    account_uuid uuid,
    last_rate_limit_at timestamptz,
    last_reset_at timestamptz,
    last_seen timestamptz NOT NULL DEFAULT now(),
    previous_session_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, machine_id),
    FOREIGN KEY (previous_session_id, machine_id) REFERENCES fleet_session_hosts (session_id, machine_id)
        ON DELETE SET NULL (previous_session_id)
);

CREATE INDEX fleet_session_hosts_process ON fleet_session_hosts (machine_id, pid, proc_start, created_at);
CREATE INDEX fleet_session_hosts_last_seen ON fleet_session_hosts (last_seen DESC, session_id);

-- The machine service is a service caller of its own, `fleet-rotator`, with a credential
-- from the exchange only (`auth/service_auth.py` `ServiceCaller`).
ALTER TABLE service_credentials DROP CONSTRAINT service_credentials_caller_check;
ALTER TABLE service_credentials ADD CONSTRAINT service_credentials_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront', 'intake',
                      'profile-fetch', 'camera', 'camera-relay', 'fleet', 'fleet-rotator'));

-- The machine whose bootstrap proof (`fleet-rotator.<machine>`) a fleet-rotator credential
-- was exchanged for. The API revokes the credential when that proof file is gone
-- (`auth/service_credentials.py` `revoke_machines_without_proof`), and core binds the
-- caller's writes to it (`fleet_machines.proof_name`).
ALTER TABLE service_credentials ADD COLUMN machine text
    CHECK (machine IS NULL OR (caller = 'fleet-rotator' AND machine ~ '^[a-z0-9][a-z0-9-]{0,62}$'));

ALTER TABLE service_caller_suspensions DROP CONSTRAINT service_caller_suspensions_caller_check;
ALTER TABLE service_caller_suspensions ADD CONSTRAINT service_caller_suspensions_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront', 'intake',
                      'profile-fetch', 'camera', 'camera-relay', 'fleet', 'fleet-rotator'));
