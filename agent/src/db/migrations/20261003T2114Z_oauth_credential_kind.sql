-- A third credential kind: a Claude Code OAuth token (`claude setup-token`), passed
-- to Claude Code as CLAUDE_CODE_OAUTH_TOKEN (src/harness/run.ts). It takes no
-- base_url, as an API key does, so the base_url check is unchanged.
ALTER TABLE ai_credentials
  DROP CONSTRAINT ai_credentials_kind_check,
  ADD CONSTRAINT ai_credentials_kind_check
    CHECK (kind IN ('anthropic_api_key', 'claude_oauth_token', 'gateway'));
