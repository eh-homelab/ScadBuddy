-- #5041 (docs/superpowers/specs/2026-10-01-distributed-tracing-design.md §5.4): an
-- AgentSession park ends and links as a classic one does (20261003T1500Z_approval_traceparent).
-- traceparent is the parked call's tool span, written by the turn that parked it
-- (sessions/turnHost.ts), so the decision can link to it. decision_traceparent is the
-- decision's `agent.approval` span, written by the respond route before it asks the
-- workflow, and carried to the response row by resolve_input (gate/store.ts), so the
-- turn that continues is the decision's child.
-- NULL: tracing was off or unsampled, a DurableSession's entry, or a row older than this file.
ALTER TABLE ai_pending_input
  ADD COLUMN traceparent text,
  ADD COLUMN decision_traceparent text;
ALTER TABLE ai_input_responses
  ADD COLUMN decision_traceparent text;
