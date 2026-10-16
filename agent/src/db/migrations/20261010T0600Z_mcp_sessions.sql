-- #2086: /mcp sessions across agent replicas. A session's transport, server and
-- subscriptions live in the process that opened it, so a request for it that
-- reaches another replica is relayed to the owner over Postgres
-- (src/mcp/sessionRelay.ts), as browser calls are (#1916, ai_bridge_messages).
--
-- ai_mcp_sessions is the directory: which replica holds each open session, and
-- what the session limits count. A session id is a credential, so the key is
-- its SHA-256, never the id. A row whose replica died is removed by the first
-- request that finds its owner silent, or swept once idle.
CREATE TABLE ai_mcp_sessions (
  id_hash     text PRIMARY KEY,
  replica     uuid NOT NULL,
  caller_key  text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  last_seen   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_mcp_sessions_caller ON ai_mcp_sessions (caller_key);
CREATE INDEX ai_mcp_sessions_last_seen ON ai_mcp_sessions (last_seen);

-- A relayed request and the parts of its answer too large for a NOTIFY payload
-- (8000 bytes, https://www.postgresql.org/docs/current/sql-notify.html), each
-- inserted in the transaction that NOTIFYs its id and taken with DELETE …
-- RETURNING by the replica it is addressed to. Rows nobody took are swept.
CREATE TABLE ai_mcp_relay_messages (
  id          uuid PRIMARY KEY,
  body        jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_mcp_relay_messages_created ON ai_mcp_relay_messages (created_at);
