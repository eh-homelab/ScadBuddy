-- #1916: browser_* calls between agent replicas. A call for a tab whose bridge
-- socket is on another replica is announced on the `scadbuddy_bridge` channel
-- with NOTIFY, and a NOTIFY payload is capped at 8000 bytes
-- (https://www.postgresql.org/docs/current/sql-notify.html), while a call's
-- arguments or a tab's result can be up to 200 000 bytes. So the request and
-- the answer are rows here, inserted in the transaction that NOTIFYs their id,
-- and the replica they are for takes each one with DELETE … RETURNING: a
-- request is run at most once, however many replicas claim its tab. Rows
-- nobody took (no replica held the tab, or the caller had stopped waiting)
-- are swept after a while. See src/bridge/relay.ts.
CREATE TABLE ai_bridge_messages (
  id          uuid PRIMARY KEY,
  body        jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_bridge_messages_created ON ai_bridge_messages (created_at);
