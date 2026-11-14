-- #1922: what each assistant turn cost, as a `turn` row of its own (one per
-- turn, written when the turn ends, src/audit/turn.ts turnCostEntry). Cost is
-- known per turn (the SDK result's `total_cost_usd`), not per tool call, so
-- the cost columns are set only on `turn` rows.
--
--   cost_usd            what the turn added to its session's spend, in USD
--   cost_priced         whether Claude Code priced it (a result came back);
--                       false for a turn that ended with no result
--   cost_estimated_usd  the part of cost_usd ScadBuddy estimated itself: the
--                       request a stopped turn was cut off in, which Claude
--                       Code never prices (src/sessions/unpricedSpend.ts, #991)
ALTER TABLE ai_audit DROP CONSTRAINT ai_audit_kind_check;
ALTER TABLE ai_audit ADD CONSTRAINT ai_audit_kind_check
  CHECK (kind IN ('tool_call', 'resource', 'approval', 'credential', 'plugin', 'settings', 'token', 'memory', 'http', 'question', 'turn'));
ALTER TABLE ai_audit ADD COLUMN cost_usd double precision CHECK (cost_usd IS NULL OR cost_usd >= 0);
ALTER TABLE ai_audit ADD COLUMN cost_priced boolean;
ALTER TABLE ai_audit ADD COLUMN cost_estimated_usd double precision CHECK (cost_estimated_usd IS NULL OR cost_estimated_usd >= 0);
