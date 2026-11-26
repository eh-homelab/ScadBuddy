-- Every fleet row says when it was made, and an agent row when it last changed (#7968).
-- fleet_channels (created_at), fleet_members (joined_at) and fleet_messages (sent_at) had
-- theirs and never change; fleet_agents and fleet_deliveries had none.
--
-- fleet_agents.created_at is when the fleet first saw the session; updated_at is when its
-- cursor last moved (an inbox read or a rewind; core's fleet routes set it with the cursor).
-- A call that only touches the row moves last_seen, the time of its latest call, and leaves
-- updated_at alone. Rows that exist get the earliest time on record for them.
ALTER TABLE fleet_agents
    ADD COLUMN created_at timestamptz,
    ADD COLUMN updated_at timestamptz;
UPDATE fleet_agents a SET created_at = LEAST(
        a.last_seen,
        (SELECT min(m.sent_at) FROM fleet_messages m WHERE m.from_session = a.session_id),
        (SELECT min(mb.joined_at) FROM fleet_members mb WHERE mb.session_id = a.session_id));
UPDATE fleet_agents SET updated_at = created_at;
ALTER TABLE fleet_agents
    ALTER COLUMN created_at SET DEFAULT now(),
    ALTER COLUMN created_at SET NOT NULL,
    ALTER COLUMN updated_at SET DEFAULT now(),
    ALTER COLUMN updated_at SET NOT NULL;

-- When the message was delivered to this recipient: its message's sent_at for the rows that
-- exist (a delivery is written in the send's own transaction).
ALTER TABLE fleet_deliveries ADD COLUMN created_at timestamptz;
UPDATE fleet_deliveries d SET created_at = m.sent_at FROM fleet_messages m WHERE m.seq = d.seq;
ALTER TABLE fleet_deliveries
    ALTER COLUMN created_at SET DEFAULT now(),
    ALTER COLUMN created_at SET NOT NULL;
