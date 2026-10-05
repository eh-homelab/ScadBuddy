-- #815 §4: the `done` summary (src/harness/attention.ts). A done request is
-- posted, never waited on: it has no timer, so `on_timeout` and `expires_at`
-- are NULL, and it outlives its turn until the user dismisses it. `summary` is
-- ScadBuddy's own record of what the turn touched (src/questions/doneSummary.ts,
-- from ai_session_resources), shown on its card beside the agent's message.
-- A replica still on an older image (a rolling deploy) inserts a done row with
-- a timer, which stays valid: the check below allows both, and the new image
-- treats a timed done row as an ordinary attention request. The reverse is NOT
-- compatible: an older image counts an untimed done row as waiting
-- (waiting_input) and cancels it when any turn of its session ends.
ALTER TABLE ai_questions
  ADD COLUMN summary text,
  DROP CONSTRAINT ai_questions_attention_check,
  ADD CONSTRAINT ai_questions_attention_check CHECK (
    (kind = 'attention') = (
      attention_reason IS NOT NULL
      AND (attention_reason = 'done' OR (on_timeout IS NOT NULL AND expires_at IS NOT NULL))
    )
  ),
  ADD CONSTRAINT ai_questions_summary_check CHECK (summary IS NULL OR attention_reason = 'done');
