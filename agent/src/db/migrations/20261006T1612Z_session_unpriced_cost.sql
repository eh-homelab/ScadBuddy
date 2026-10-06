-- #991: what a session was charged for model requests Claude Code never
-- priced. A request cut off mid-stream (Stop, or a turn that died) ends with
-- its usage seen in the stream but no cost: the SDK's result reports 0 for it,
-- and the `cost-state` it writes to the transcript leaves it out. The manager
-- prices it from the stream (sessions/unpricedSpend.ts) and adds it to
-- cost_usd; this column remembers how much of cost_usd that is, because a
-- RESUMED query's `total_cost_usd` restores the transcript's total, which does
-- not hold it (sessions/manager.ts finish).
ALTER TABLE ai_sessions
  ADD COLUMN unpriced_cost_usd double precision NOT NULL DEFAULT 0 CHECK (unpriced_cost_usd >= 0);
