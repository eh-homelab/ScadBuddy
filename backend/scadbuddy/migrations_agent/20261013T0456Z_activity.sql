-- Activity (spec 2026-10-09 principal jobs §4, §5): who started each agent operation, and
-- the agent's activity kinds as data the backend reads (it never writes them).
ALTER TABLE ai_operations ADD COLUMN principal text, ADD COLUMN session text, ADD COLUMN via_tool text;
CREATE TABLE ai_activity_kinds (
  key        text PRIMARY KEY CHECK (key LIKE 'agent.%'),
  label      text NOT NULL,
  facets     jsonb NOT NULL DEFAULT '[]'::jsonb,
  retired_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now()
);
