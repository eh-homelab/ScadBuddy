-- Which workflow runs a durable session, fixed when its workflow is first started
-- (unified runtime spec 2026-10-13 §5.1, §10): NULL until the first send, then
-- 'durable' (agent-durable's DurableSession on the `agent` queue) or 'unified'
-- (AgentSession on `agent-sessions`). A session keeps its runtime for life, so a
-- send, a fork or a gate call is routed by this column, never by the
-- `session_runtime` setting as it stands later.
ALTER TABLE ai_sessions ADD COLUMN runtime text CHECK (runtime IN ('durable', 'unified'));
