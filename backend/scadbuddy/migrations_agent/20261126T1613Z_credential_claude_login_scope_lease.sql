-- #7983 (design 2026-11-26-fleet-session-registry-and-rotator-design.md §6.1): the shared
-- credential pool learns what the fleet's login rotator needs.
--
--   kind 'claude_login'  a whole Claude Code `claudeAiOauth` object, sealed as every other
--                        kind (the AAD binds the row id and the kind). ScadBuddy's agent
--                        never uses one.
--   scope                who may use the row: 'agent' (ScadBuddy's agent), 'fleet' (the
--                        machines' login rotator) or 'both'. Every existing row is 'agent',
--                        so no row changes meaning.
--   account_uuid, organization_uuid, subscription_type, rate_limit_tier, email
--                        the login's account, in clear, so a person and the fleet can name it
--                        without a reveal. Not secret. Set only on a 'claude_login' row.
--   leased_to, leased_at the machine that holds the login now, and since when. At most one
--                        machine holds a login, because a refresh rotates its refresh token.
--                        Only a 'claude_login' row holds a lease.
--   lease_epoch          bumped by each lease change (#7984), so a stale holder's write
--                        is refused.
ALTER TABLE ai_credentials
  DROP CONSTRAINT ai_credentials_kind_check,
  ADD CONSTRAINT ai_credentials_kind_check
    CHECK (kind IN ('anthropic_api_key', 'claude_login', 'claude_oauth_token', 'gateway')),
  ADD COLUMN scope text NOT NULL DEFAULT 'agent',
  ADD CONSTRAINT ai_credentials_scope_check CHECK (scope IN ('agent', 'fleet', 'both')),
  ADD COLUMN account_uuid      text,
  ADD COLUMN organization_uuid text,
  ADD COLUMN subscription_type text,
  ADD COLUMN rate_limit_tier   text,
  ADD COLUMN email             text,
  ADD CONSTRAINT ai_credentials_account_check CHECK (
    kind = 'claude_login' OR (account_uuid IS NULL AND organization_uuid IS NULL
      AND subscription_type IS NULL AND rate_limit_tier IS NULL AND email IS NULL)
  ),
  ADD CONSTRAINT ai_credentials_account_length_check CHECK (
    char_length(account_uuid) <= 200 AND char_length(organization_uuid) <= 200
    AND char_length(subscription_type) <= 200 AND char_length(rate_limit_tier) <= 200
    AND char_length(email) <= 320
  ),
  ADD COLUMN leased_to   text,
  ADD COLUMN leased_at   timestamptz,
  ADD COLUMN lease_epoch bigint NOT NULL DEFAULT 0,
  ADD CONSTRAINT ai_credentials_lease_check CHECK (
    (leased_to IS NULL) = (leased_at IS NULL) AND (leased_to IS NULL OR kind = 'claude_login')
  );

-- Two machines can each hold their own login to one account, so account_uuid is not unique.
-- A rate limit cools every login row of the account (#7984), which this index finds.
CREATE INDEX ai_credentials_login_account ON ai_credentials (account_uuid) WHERE kind = 'claude_login';
