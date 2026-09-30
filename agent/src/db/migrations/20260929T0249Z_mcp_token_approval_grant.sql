-- #300: the per-token approval grant (spec §6: "Approvals of outward actions by
-- another agent are off by default and need a per-token grant"; §8.2: "Only the
-- browser user decides, or another principal with a per-token grant"). See
-- src/auth/tokens.ts and src/approvals/service.ts `authorize`.
--
-- Off for every existing token. Only an `outward` token can hold it: deciding
-- an outward action is at least as much as taking one, and the sessions_approve
-- and sessions_deny tools need the `outward` tier (src/tools/sessions.ts).
ALTER TABLE ai_mcp_tokens
  ADD COLUMN approval_grant boolean NOT NULL DEFAULT false,
  ADD CONSTRAINT ai_mcp_tokens_grant_outward CHECK (NOT approval_grant OR tier = 'outward');
