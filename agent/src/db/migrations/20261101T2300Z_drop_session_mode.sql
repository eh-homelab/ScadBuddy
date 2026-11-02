-- Unified runtime plan Phase 10 (#2726, #5171): one runtime. durable_offset was the Python
-- follower's cursor; mode was the classic/durable switch; turn_id/lease_until was classic's
-- claim, NULL on every row since plan Step 6.3 (the CHECK that paired them goes with them:
-- Postgres drops a table constraint with a column it names). The previous release (#5174)
-- neither reads nor writes any of them, so the drop is safe in a rolling deploy.
-- ai_questions stays: it holds the `done` summaries (gate/questions.ts).
ALTER TABLE ai_sessions
  DROP COLUMN durable_offset,
  DROP COLUMN mode,
  DROP COLUMN turn_id,
  DROP COLUMN lease_until;
