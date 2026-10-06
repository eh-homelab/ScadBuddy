-- #988 (docs/superpowers/specs/2026-10-01-distributed-tracing-design.md §5.4):
-- approvals end and link. A parked call's tool span ends when it parks; its
-- W3C traceparent is stored here, so the decision, its own trace, can link to
-- it. decision_traceparent is that decision's `agent.approval` span, written
-- with the decision, so the turn that continues (on whichever replica parked
-- it, or a resumed orphan's after a restart) is the decision's child.
-- NULL: tracing was off or unsampled then, or the row predates this file.
ALTER TABLE ai_approvals
  ADD COLUMN traceparent text,
  ADD COLUMN decision_traceparent text;
