-- #1383: 20261005T1200Z_attention_done's summary and unattended checks read
-- `attention_reason = 'done'`, which is NULL, not false, on a plain question
-- (kind 'question', attention_reason NULL), and a CHECK passes on NULL. So a
-- question row could carry a summary or unattended = true, and the done
-- supersede rule (questions/service.ts) reads `unattended`. NULL-safe now.
-- A separate migration because that one may already have applied.
ALTER TABLE ai_questions
  DROP CONSTRAINT ai_questions_summary_check,
  DROP CONSTRAINT ai_questions_unattended_check,
  ADD CONSTRAINT ai_questions_summary_check CHECK (summary IS NULL OR attention_reason IS NOT DISTINCT FROM 'done'),
  ADD CONSTRAINT ai_questions_unattended_check CHECK (NOT unattended OR attention_reason IS NOT DISTINCT FROM 'done');
