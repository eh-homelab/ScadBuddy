-- #300 (PR #715 review): the tiers of the turn that asked for an approval.
-- A turn sent through the `sessions_*` tools runs with its sender's tiers
-- (sessions/manager.ts SendOptions.tiers), which an `Owner` does not carry.
-- An orphan approved after a restart resumes in a new turn
-- (manager.ts `resumeApproved`); without these, that turn would fall back to
-- the owner's default (`read` for any MCP principal) and not even be offered
-- the approved tool. NULL: the turn ran with the owner's default (the browser
-- user's, or a prepare/confirm approval outside a session).
--
-- Existing approvals keep NULL, so resuming one behaves as before.
ALTER TABLE ai_approvals
  ADD COLUMN requested_tiers text[],
  ADD CONSTRAINT ai_approvals_requested_tiers
    CHECK (requested_tiers <@ ARRAY['read', 'write', 'outward']::text[]);
