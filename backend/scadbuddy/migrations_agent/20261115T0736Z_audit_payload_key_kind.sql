-- #5299 step 4 (#5342): the API releases a durable subject's payload data key to the
-- agent (backend agent_data/payload_keys.py), and each release is an audit row of its
-- own kind, `payload_key`, apart from the `credential` reveals: a key is fetched at
-- transcript-read rate, so a release is written once per subject and window
-- (`RELEASE_AUDIT_WINDOW`), a key made or refused every time.
ALTER TABLE ai_audit DROP CONSTRAINT ai_audit_kind_check;
ALTER TABLE ai_audit ADD CONSTRAINT ai_audit_kind_check
  CHECK (kind IN ('tool_call', 'resource', 'approval', 'credential', 'plugin', 'settings', 'token', 'memory', 'http', 'question', 'turn', 'payload_key'));
