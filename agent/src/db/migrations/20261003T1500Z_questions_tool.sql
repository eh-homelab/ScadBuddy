-- #940/#1075: which tool asked: AskUserQuestion (the session's agent) or
-- mcp__scadbuddy_questions__ask_user (a subagent, src/harness/questions.ts).
-- Every question before this column was an AskUserQuestion call. The default
-- stays: a replica still on an older image (a rolling deploy, a rollback)
-- inserts without the column, and its questions are AskUserQuestion calls.
ALTER TABLE ai_questions ADD COLUMN tool text NOT NULL DEFAULT 'AskUserQuestion';
