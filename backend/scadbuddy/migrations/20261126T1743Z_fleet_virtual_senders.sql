-- Virtual senders (#8026, #7971 part 6): reserved one-way names in the agent fleet chat. Only
-- a service caller posts as one (`fleet-rotator`, core's `…/crud/fleet-rotator/virtual-messages`),
-- and a DM to one is refused with its reject_message (reason `virtual-sender`).
--
-- A name follows the channel and label rule (^[a-z0-9-]{1,40}$), and it holds at least one
-- character that is not a lower-case hex digit. An agent's handle is 8 lower-case hex
-- characters (the start of its session id), so no handle can ever equal a name.
CREATE TABLE fleet_virtual_senders (
    name text PRIMARY KEY CHECK (name ~ '^[a-z0-9-]{1,40}$' AND name !~ '^[0-9a-f]*$'),
    description text NOT NULL,
    reject_message text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO fleet_virtual_senders (name, description, reject_message) VALUES
    ('rotator',
     'The machine service that moves a machine''s Claude login to another account at a usage limit (#7990). It tells each session that stopped on the limit to continue.',
     '"rotator" is the login rotator, a virtual sender: it only sends messages and reads none. Tell a person in a channel, or DM a session by its handle.'),
    ('github',
     'The GitHub stream (#7971 part 7): it posts GitHub events of the watched repositories into the fleet''s channels.',
     '"github" is the GitHub stream, a virtual sender: it only posts GitHub events and reads no messages. Comment on the issue or PR on GitHub instead.');

-- A message is from a session or from a virtual sender, never both.
ALTER TABLE fleet_messages ALTER COLUMN from_session DROP NOT NULL;
ALTER TABLE fleet_messages ADD COLUMN from_sender text REFERENCES fleet_virtual_senders;
ALTER TABLE fleet_messages ADD CONSTRAINT fleet_messages_one_sender
    CHECK (num_nonnulls(from_session, from_sender) = 1);

-- The machine service is a service caller of its own, `fleet-rotator`, with a credential
-- from the exchange only (`auth/service_auth.py` `ServiceCaller`). The same lists as #8003's
-- migration, so either one may apply last.
ALTER TABLE service_credentials DROP CONSTRAINT service_credentials_caller_check;
ALTER TABLE service_credentials ADD CONSTRAINT service_credentials_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront', 'intake',
                      'profile-fetch', 'camera', 'camera-relay', 'fleet', 'fleet-rotator'));

ALTER TABLE service_caller_suspensions DROP CONSTRAINT service_caller_suspensions_caller_check;
ALTER TABLE service_caller_suspensions ADD CONSTRAINT service_caller_suspensions_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront', 'intake',
                      'profile-fetch', 'camera', 'camera-relay', 'fleet', 'fleet-rotator'));
