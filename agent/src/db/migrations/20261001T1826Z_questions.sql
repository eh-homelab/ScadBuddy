-- #940: questions the agent asked the user with AskUserQuestion
-- (src/harness/questions.ts, src/questions/service.ts). One row per call that
-- waited for an answer; pending while outcome IS NULL. A pending row belongs
-- to the turn that parked on it, and is cancelled when that turn ends.
CREATE TABLE ai_questions (
  id                uuid PRIMARY KEY,
  session_id        uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  turn_id           uuid NOT NULL,
  -- The tool_use block's id (the panel's tool.call id).
  tool_use_id       text NOT NULL,
  -- The questions as the panel shows them: the turn's secrets redacted.
  questions         jsonb NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  outcome           text CHECK (outcome IN ('answered', 'cancelled')),
  -- One string per question, in order: what the user chose or typed.
  answers           jsonb,
  answered_by_kind  text,
  answered_by_id    text,
  answered_by_label text,
  resolved_at       timestamptz,
  -- Why it was cancelled.
  reason            text,
  CHECK ((outcome IS NULL) = (resolved_at IS NULL)),
  CHECK ((outcome = 'answered') = (answers IS NOT NULL))
);
CREATE INDEX ai_questions_session ON ai_questions (session_id, created_at);
CREATE INDEX ai_questions_pending ON ai_questions (session_id) WHERE outcome IS NULL;
