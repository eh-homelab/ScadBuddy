-- #7323: every session names its runtime. A row with none is a classic session an earlier
-- release left; its next send would have recorded 'unified' (agent_data/turns.py, before
-- #7323), and its transcript is in ai_session_entries, which an AgentSession resumes, so it
-- is recorded now. Prod on 2026-11-23 had 147 such rows (138 idle, 9 done) and none running.
-- Every insert since plan Step 6.3 names 'unified' (sessions/manager.ts, agent_data/sessions.py),
-- so the build before this one writes no NULL during the roll; the default is for an insert
-- that names no runtime. A 'durable' row stays: it is refused `closed` (sessions/durable.ts).
--
-- The build before this one runs beside it. Its claim's coalesce(runtime, 'unified') and its
-- reads hold for a row that names its runtime. The lock comes first, so the UPDATE's row locks
-- are never upgraded under a transaction of that build which waits for one of them.
LOCK TABLE ai_sessions IN ACCESS EXCLUSIVE MODE;
UPDATE ai_sessions SET runtime = 'unified' WHERE runtime IS NULL;
ALTER TABLE ai_sessions
  ALTER COLUMN runtime SET DEFAULT 'unified',
  ALTER COLUMN runtime SET NOT NULL;
