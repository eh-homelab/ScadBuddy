-- #823: a fork spends from its parent's budget rather than getting one of its
-- own. budget_root_id names the session whose budget a fork spends from: its
-- parent's root, so a fork of a fork shares the same one. NULL means the
-- session is its own root, which every existing session is (forks included:
-- they keep the budget they were given) and which a replica still on an older
-- image (a rolling deploy) goes on inserting. The pool's budget is the root's
-- budget_usd (a member's own is only a fallback should the root row ever go),
-- and its spend is the sum of cost_usd over every session with that root
-- (sessions/manager.ts POOL_BUDGET, POOL_COST).
ALTER TABLE ai_sessions ADD COLUMN budget_root_id uuid;
CREATE INDEX ai_sessions_budget_root ON ai_sessions ((coalesce(budget_root_id, id)));
