-- #815: attention requests (src/harness/attention.ts) park at the same gate as
-- questions (#940) and are stored beside them: an `answer`-kind entry of the
-- tool-call gate (durable-agents spec §6.6), shown on the same card. Unlike a
-- question, an attention request has a timer: `expires_at`, and `on_timeout`
-- for what the timer does. The timer never answers: it resolves the row as
-- `timed_out`, which the agent reads as "nobody replied", never as a choice.
-- Every row before this migration is a question, as is a row from a replica
-- still on an older image (a rolling deploy), which inserts without the columns.
ALTER TABLE ai_questions
  ADD COLUMN kind text NOT NULL DEFAULT 'question' CHECK (kind IN ('question', 'attention')),
  ADD COLUMN attention_reason text CHECK (attention_reason IN ('tab_disconnected', 'question', 'blocked', 'done')),
  ADD COLUMN on_timeout text CHECK (on_timeout IN ('proceed', 'wait', 'stop')),
  ADD COLUMN expires_at timestamptz,
  ADD CONSTRAINT ai_questions_attention_check CHECK (
    (kind = 'attention') = (attention_reason IS NOT NULL AND on_timeout IS NOT NULL AND expires_at IS NOT NULL)
  );
ALTER TABLE ai_questions DROP CONSTRAINT ai_questions_outcome_check;
-- `timed_out` is an attention request's only: a question has no timer.
ALTER TABLE ai_questions ADD CONSTRAINT ai_questions_outcome_check
  CHECK (outcome IN ('answered', 'cancelled', 'timed_out') AND (outcome <> 'timed_out' OR kind = 'attention'));
-- #815's throttle (one open request per session per reason) and per-user rate limit read these.
CREATE INDEX ai_questions_attention ON ai_questions (created_at) WHERE kind = 'attention';
