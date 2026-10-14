-- #251: MCP bearer tokens (spec §8.1, §8.3, §9). See src/auth/tokens.ts.
-- One row per token minted in Settings. The token itself is never stored:
-- only its SHA-256 (hex), which verify() looks up (spec §8.1 "stored hashed").
CREATE TABLE ai_mcp_tokens (
  id           uuid PRIMARY KEY,
  name         text NOT NULL,
  tier         text NOT NULL CHECK (tier IN ('read', 'write', 'outward')),
  token_hash   text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz,
  revoked_at   timestamptz,
  last_used_at timestamptz
);
