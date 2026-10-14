-- #1383: 20261005T1200Z_attention_done's summary and unattended checks read
-- `attention_reason = 'done'`, which is NULL, not false, on a plain question
-- (kind 'question', attention_reason NULL), and a CHECK passes on NULL. So a
-- question row could carry a summary or unattended = true, and the done
-- supersede rule (questions/service.ts) reads `unattended`. NULL-safe now.
-- A separate migration because that one may already have applied.
--
-- A database that took the loose checks may hold a row the strict ones refuse,
-- and validating against it would fail this migration and stop the agent at
-- start. Such a row is not a done request, so neither field means anything on
-- it: clear them, then add the checks NOT VALID and validate them, so the scan
-- runs against rows that already conform.
UPDATE ai_questions SET summary = NULL, unattended = false
WHERE attention_reason IS DISTINCT FROM 'done' AND (summary IS NOT NULL OR unattended);
ALTER TABLE ai_questions
  DROP CONSTRAINT ai_questions_summary_check,
  DROP CONSTRAINT ai_questions_unattended_check,
  ADD CONSTRAINT ai_questions_summary_check CHECK (summary IS NULL OR attention_reason IS NOT DISTINCT FROM 'done') NOT VALID,
  ADD CONSTRAINT ai_questions_unattended_check CHECK (NOT unattended OR attention_reason IS NOT DISTINCT FROM 'done') NOT VALID;
ALTER TABLE ai_questions VALIDATE CONSTRAINT ai_questions_summary_check;
ALTER TABLE ai_questions VALIDATE CONSTRAINT ai_questions_unattended_check;
