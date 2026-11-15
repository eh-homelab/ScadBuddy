-- The tool-call gate's durable side (spec 2026-10-01 §6.6, #1056). A durable
-- session's parked calls live in its workflow; `ai_pending_input` is their
-- projection, which GET /api/v1/ai/pending-input (the badge) reads in one query,
-- and `ai_input_responses` holds every resolution's outcome, the answer kinds'
-- answer among them, which the tool's activity on `agent-tools` returns as its
-- result. The agent-durable worker's open_input and resolve_input activities are
-- the only writers, besides the agent service's orphan sweep (src/gate/sweep.ts);
-- each removes a row through one guarded DELETE … RETURNING, so a request is
-- resolved once. Classic entries stay in ai_approvals and ai_questions.
--
-- Both go with their session: deleting a session's rows (forgetSubject, §6.5)
-- removes its questions and answers, which are stored in plaintext.
CREATE TABLE ai_pending_input (
  -- durable:<session id>:<workflow run id>:<tool_use_id>, opaque to clients.
  request_id      text PRIMARY KEY,
  session_id      uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  workflow_id     text NOT NULL,
  workflow_run_id text NOT NULL,
  kind            text NOT NULL CHECK (kind IN ('approval', 'answer')),
  tool            text NOT NULL,
  -- An approval's scrubbed summary and input hash, never its input; empty for an answer.
  summary         text NOT NULL DEFAULT '',
  input_hash      text,
  -- An answer's question or attention message, scrubbed and at most 16 KiB; empty for an approval.
  prompt          text NOT NULL DEFAULT '',
  -- {kind, id, label}: the principal the turn ran for.
  requested_by    jsonb,
  responders      text[] NOT NULL,
  -- An attention request's {reason, on_timeout}; null otherwise.
  attention       jsonb,
  created_at      timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz NOT NULL,
  -- When the sweep last found this row's run open (it describes a run at most every 10 minutes).
  last_checked_at timestamptz,
  CHECK (expires_at <= created_at + interval '86400 seconds')
);
CREATE INDEX ai_pending_input_session ON ai_pending_input (session_id);
CREATE INDEX ai_pending_input_run ON ai_pending_input (workflow_id, workflow_run_id);

CREATE TABLE ai_input_responses (
  request_id  text PRIMARY KEY,
  session_id  uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  kind        text NOT NULL CHECK (kind IN ('approval', 'answer')),
  outcome     text NOT NULL CHECK (outcome IN ('approved', 'denied', 'expired', 'answered', 'cancelled', 'timed_out')),
  -- An answer's response; null for every other outcome.
  response    jsonb,
  -- {kind, id, label}; the system ({kind: 'system', …}) for a timer, a cancel or the sweep.
  responder   jsonb NOT NULL,
  reason      text,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_input_responses_session ON ai_input_responses (session_id);

-- A durable approval has no ai_approvals row and no uuid: its `approval` audit
-- row and the `tool_call` row of the call it let run both carry its request id,
-- and the tool_call row's approver is copied from ai_input_responses by it.
ALTER TABLE ai_audit ADD COLUMN request_id text;
CREATE INDEX ai_audit_request ON ai_audit (request_id) WHERE request_id IS NOT NULL;
