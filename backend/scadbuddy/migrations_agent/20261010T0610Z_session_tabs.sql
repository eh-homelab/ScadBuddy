-- #2086: which tab a chat session's browser_* calls go to, shared by every agent
-- replica. The chat socket pairs a session with the tab the user sends from
-- (bridge/hub.ts `pairSession`) on the replica that socket reached, but a
-- durable session's tool calls run as activities on `agent-tools`, which every
-- replica polls, so the call can run on a replica that never saw the message.
-- It reads the pairing here, then reaches the tab through the bridge relay.
CREATE TABLE ai_session_tabs (
  session_id  uuid PRIMARY KEY REFERENCES ai_sessions (id) ON DELETE CASCADE,
  tab_id      text NOT NULL,
  paired_at   timestamptz NOT NULL DEFAULT now()
);
