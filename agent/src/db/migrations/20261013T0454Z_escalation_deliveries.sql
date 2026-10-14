-- Which flow-own calls waiting at a run's starter session (#2482, spec 2026-10-11 §4.4)
-- the agent has sent that session: `workflow_runs` is the backend's, so the agent keeps
-- its own record of what it delivered (a child's entry carries `delivered_at` instead).
-- A row is claimed before the turn is sent and removed if the send did not start, so
-- replicas never deliver one call twice.
CREATE TABLE ai_escalation_deliveries (
  -- flow:<run>:<workflow run>:<call>
  request_id   text PRIMARY KEY,
  session_id   uuid NOT NULL,
  delivered_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ai_escalation_deliveries_at ON ai_escalation_deliveries (delivered_at);
