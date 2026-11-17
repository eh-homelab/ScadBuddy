-- #6449: a durable turn's spend through the API (`…/sessions/{id}/turn/spend`,
-- api/crud_agent_turns.py) is applied once per key: a placement sends each write with a key of
-- its own (a token of the placement's own and a sequence number) and sends the same key again when it never heard
-- the answer, so a write that committed while its answer was lost is not added twice. A key is
-- kept an hour, far past a placement's resend; the route deletes the session's older keys.
CREATE TABLE ai_turn_spend_keys (
  session_id uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  key        text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, key)
);
