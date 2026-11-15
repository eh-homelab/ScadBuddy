-- An AgentSession turn's placements (unified runtime spec 2026-10-13 §5.2, §5.5):
-- `turn_attempt` is the placement that holds the running turn now (the workflow's run id and
-- cycle), written by each `session_turn` as it claims the turn. An attempt the workflow gave up
-- on and moved (a stalled heartbeat) sees another's here and stops: no more events, cost or
-- transcript from it.
-- `turn_mark_turn` and `turn_mark` are the turn the mark is for and the last main-transcript
-- entry (ai_session_entries.id) before that turn's first placement: a later placement resumes
-- with the restart message only when the turn's prompt landed after it, else it sends the
-- message.
ALTER TABLE ai_sessions
  ADD COLUMN turn_attempt text,
  ADD COLUMN turn_mark_turn text,
  ADD COLUMN turn_mark bigint;
