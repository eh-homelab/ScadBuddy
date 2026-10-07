-- Durable sessions (#1056, final fix wave): every message a durable send takes is
-- committed here BEFORE Temporal is asked anything. The send's Update only nudges the
-- DurableSession workflow with the id; the workflow loads the session's pending messages
-- from this table (agent-durable inputs.py) when a run starts and when nudged, so a lost
-- Update, or a Temporal restart while no worker runs, loses nothing.
--   id       the turn id: the Update's updateId, and the workflow's idempotency key
--   seq      the order the session's messages run in
--   context  the panel's page context (model-only, as classic sends it)
--   note     a model-only line the agent service adds when it restores a run from a
--            snapshot (the calls whose results were lost)
--   status   pending: not run yet; run: a run took it (agent-durable marks it, as one
--            compare-and-set, just before the turn starts); abandoned: it will never run
--            (a Stop, or a refusal), and the log says so
CREATE TABLE ai_durable_inputs (
  id         uuid PRIMARY KEY,
  session_id uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  seq        bigint GENERATED ALWAYS AS IDENTITY,
  text       text NOT NULL,
  context    text,
  note       text,
  status     text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'run', 'abandoned')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_durable_inputs_pending ON ai_durable_inputs (session_id, seq) WHERE status = 'pending';
-- The sender's heartbeat: bumped before every delivery attempt, so another replica can
-- tell a send that is gone (unchanged for long, on its monotonic clock) and take it over.
ALTER TABLE ai_durable_streams ADD COLUMN send_attempt bigint NOT NULL DEFAULT 0;
