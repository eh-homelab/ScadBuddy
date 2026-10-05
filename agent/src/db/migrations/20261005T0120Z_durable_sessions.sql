-- Durable sessions (spec 2026-10-01 §6.2), written by the agent-durable sidecar.
-- One row per segment attempt: its cost (every attempt really spent it) and the
-- Claude session id it ran in, so forgetSubject finds every ai_session_entries key.
CREATE TABLE ai_durable_segments (
  session_id        uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  segment_index     integer NOT NULL,
  attempt           integer NOT NULL,
  claude_session_id text NOT NULL,
  cost_usd          double precision NOT NULL DEFAULT 0,
  created_at        timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, segment_index, attempt)
);
-- The projector's place in each session's live output, and its lease (one
-- follow_agent subscriber per session across replicas).
CREATE TABLE ai_durable_streams (
  session_id  uuid PRIMARY KEY REFERENCES ai_sessions (id) ON DELETE CASCADE,
  next_offset bigint NOT NULL DEFAULT 0,
  holder      text,
  lease_until timestamptz
);
-- The plugin's AgentState as of the latest segment or tool call (plan ruling 15), so an
-- execution that closed without handing over (terminated, failed) can be resumed.
CREATE TABLE ai_durable_snapshots (
  session_id uuid PRIMARY KEY REFERENCES ai_sessions (id) ON DELETE CASCADE,
  version    bigint NOT NULL,
  state      text NOT NULL,
  in_flight  jsonb NOT NULL DEFAULT '[]',
  saved_at   timestamptz NOT NULL DEFAULT now()
);
