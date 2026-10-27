-- #4780: which agent replica holds each browser tab's bridge socket, so a browser_*
-- call made on any replica runs as an activity on that replica's own queue
-- (`agent-tools.<identity>`, main.ts HOST_QUEUE) through Temporal, not through
-- Postgres rows and NOTIFY (src/bridge/relay.ts). A replica claims a tab when its
-- socket says hello and releases it, only while the row still names its own queue,
-- when the socket closes: a reconnect to another replica keeps the newer claim.
-- A replica that died leaves its rows; a call to one fails to start within the ack
-- timeout and drops the row, and a replica drops its own queue's rows at start.
CREATE TABLE ai_tab_owners (
  tab_id      text PRIMARY KEY,
  queue       text NOT NULL,
  claimed_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_tab_owners_queue ON ai_tab_owners (queue);
