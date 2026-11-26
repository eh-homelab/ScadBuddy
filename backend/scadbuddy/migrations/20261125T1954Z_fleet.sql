-- The agent fleet chat (spec 2026-11-25-agent-fleet-messaging-design §4.1): who is in the
-- fleet, its channels, and the messages with one delivery row per recipient. The fleet MCP
-- server reaches them only through core's fleet routes.
CREATE TABLE fleet_agents (
    session_id uuid PRIMARY KEY,
    handle text NOT NULL UNIQUE,
    label text,
    cursor bigint NOT NULL DEFAULT 0,
    last_seen timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE fleet_channels (
    name text PRIMARY KEY CHECK (name ~ '^[a-z0-9-]{1,40}$'),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE fleet_members (
    channel text REFERENCES fleet_channels ON DELETE CASCADE,
    session_id uuid REFERENCES fleet_agents ON DELETE CASCADE,
    joined_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, session_id)
);

CREATE TABLE fleet_messages (
    seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_session uuid NOT NULL REFERENCES fleet_agents,
    channel text REFERENCES fleet_channels,
    to_session uuid REFERENCES fleet_agents,
    text text NOT NULL,
    sent_at timestamptz NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(channel, to_session) = 1)
);

CREATE TABLE fleet_deliveries (
    session_id uuid REFERENCES fleet_agents ON DELETE CASCADE,
    seq bigint REFERENCES fleet_messages ON DELETE CASCADE,
    PRIMARY KEY (session_id, seq)
);

-- The fleet MCP server is a service caller, `fleet`, with a credential from the exchange only
-- (`auth/service_auth.py` `ServiceCaller`).
ALTER TABLE service_credentials DROP CONSTRAINT service_credentials_caller_check;
ALTER TABLE service_credentials ADD CONSTRAINT service_credentials_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront', 'intake',
                      'profile-fetch', 'camera', 'camera-relay', 'fleet'));

ALTER TABLE service_caller_suspensions DROP CONSTRAINT service_caller_suspensions_caller_check;
ALTER TABLE service_caller_suspensions ADD CONSTRAINT service_caller_suspensions_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront', 'intake',
                      'profile-fetch', 'camera', 'camera-relay', 'fleet'));
