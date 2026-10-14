-- #818: automatic Hindsight recall and retain (src/memory/hindsight.ts) run as
-- SDK hooks, not tool calls, so they get audit rows of their own kind.
ALTER TABLE ai_audit DROP CONSTRAINT ai_audit_kind_check;
ALTER TABLE ai_audit ADD CONSTRAINT ai_audit_kind_check
  CHECK (kind IN ('tool_call', 'resource', 'approval', 'credential', 'plugin', 'settings', 'token', 'memory'));
