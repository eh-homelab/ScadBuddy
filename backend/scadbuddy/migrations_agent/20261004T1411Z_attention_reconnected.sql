-- #815 §2: a `tab_disconnected` attention request is resolved as `reconnected`
-- when its session gets a connected ScadBuddy tab again (bridge/hub.ts, by the
-- system, never as an answer). Only that reason can end this way.
ALTER TABLE ai_questions DROP CONSTRAINT ai_questions_outcome_check;
ALTER TABLE ai_questions ADD CONSTRAINT ai_questions_outcome_check
  CHECK (
    outcome IN ('answered', 'cancelled', 'timed_out', 'reconnected')
    AND (outcome <> 'timed_out' OR kind = 'attention')
    AND (outcome <> 'reconnected' OR coalesce(attention_reason, '') = 'tab_disconnected')
  );
