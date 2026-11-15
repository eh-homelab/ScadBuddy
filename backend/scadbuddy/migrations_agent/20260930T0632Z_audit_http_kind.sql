-- #827: every request the assistant's http_request tool makes
-- (src/harness/httpRequest.ts) is an audit row of its own kind.
ALTER TABLE ai_audit DROP CONSTRAINT ai_audit_kind_check;
ALTER TABLE ai_audit ADD CONSTRAINT ai_audit_kind_check
  CHECK (kind IN ('tool_call', 'resource', 'approval', 'credential', 'plugin', 'settings', 'token', 'memory', 'http'));
