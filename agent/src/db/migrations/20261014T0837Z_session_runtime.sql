-- Which workflow runs a durable session, fixed when its workflow is first started
-- (unified runtime spec 2026-10-13 §5.1, §10): NULL until the first send, then
-- 'durable' (agent-durable's DurableSession on the `agent` queue) or 'unified'
-- (AgentSession on `agent-sessions`). A session keeps its runtime for life, so a
-- send, a fork or a gate call is routed by this column, never by the
-- `session_runtime` setting as it stands later.
ALTER TABLE ai_sessions ADD COLUMN runtime text CHECK (runtime IN ('durable', 'unified'));
-- Every durable session that exists now has, or may have, a DurableSession workflow (an
-- accepted first send with no output yet, a fork child, an early-failed turn all leave
-- durable_offset at 0), so none may be taken for a new one by its next send.
UPDATE ai_sessions SET runtime = 'durable' WHERE mode = 'durable' AND runtime IS NULL;
