-- #5870: whether a session's turns are offered the session's background built-ins
-- (CronCreate, CronDelete, CronList, ScheduleWakeup, Monitor, Workflow, TaskStop;
-- src/access/nativeBuiltins.ts SESSION_BUILTINS). False for a session a flow run started
-- and for a Workflow agent's chat (SessionManager.startChild), so neither can schedule later
-- turns or start nested Workflows without a person; a fork keeps its parent's. Every other
-- session, and every session before this, is true. A session a flow run forked, and one such a
-- session starts or forks through the sessions_* tools, is false too.
--
-- turn_flow: whether the running turn was sent by a flow run, or by such a session (durable.ts
-- `send`); such a turn
-- is offered no background built-in either, whatever the session's background_tools says.
ALTER TABLE ai_sessions
  ADD COLUMN background_tools boolean NOT NULL DEFAULT true,
  ADD COLUMN turn_flow boolean NOT NULL DEFAULT false;
