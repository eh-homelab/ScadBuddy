-- #4780: browser_* calls between replicas go through Temporal to the replica holding
-- the tab (src/bridge/relay.ts, ai_tab_owners), so the Postgres relay's rows
-- (#1916, 20261009T1900Z_bridge_messages.sql) and its `scadbuddy_bridge` channel
-- are no longer used. During a rollout, a replica that still runs the old relay
-- cannot forward a call any more; that call answers with an error.
DROP TABLE IF EXISTS ai_bridge_messages;
