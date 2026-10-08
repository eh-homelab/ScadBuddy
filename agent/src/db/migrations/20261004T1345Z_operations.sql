-- The agent's commands (spec 2026-10-01 §4.2 "Our record", #1055): one row per
-- execution of an AgentOperation workflow on `agent-tools`, written only by its
-- activities. The backend's `operations`, in the agent's own schema (it owns ai_*).
CREATE TABLE ai_operations (
  id              text PRIMARY KEY,
  kind            text NOT NULL,
  subject         text NOT NULL,
  operation_key   text NOT NULL,
  status          text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
  request         jsonb NOT NULL,
  result          jsonb,
  error           jsonb,
  workflow_id     text NOT NULL,
  workflow_run_id text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  finished_at     timestamptz,
  CHECK ((status = 'running') = (finished_at IS NULL))
);
CREATE UNIQUE INDEX ai_operations_execution ON ai_operations (workflow_id, workflow_run_id);
CREATE INDEX ai_operations_operation_key ON ai_operations (operation_key, created_at DESC);
CREATE INDEX ai_operations_finished ON ai_operations (finished_at) WHERE finished_at IS NOT NULL;
