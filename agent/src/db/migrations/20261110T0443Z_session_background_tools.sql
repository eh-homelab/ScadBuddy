-- #5870: whether a session's turns are offered the session's background built-ins
-- (CronCreate, CronDelete, CronList, ScheduleWakeup, Monitor, Workflow, TaskStop;
-- src/access/nativeBuiltins.ts SESSION_BUILTINS). False for a session a flow run started
-- and for a Workflow agent's chat (SessionManager.startChild), so neither can schedule later
-- turns or start nested Workflows without a person; a fork keeps its parent's. Every other
-- session, and every session before this, is true.
ALTER TABLE ai_sessions
  ADD COLUMN background_tools boolean NOT NULL DEFAULT true;
