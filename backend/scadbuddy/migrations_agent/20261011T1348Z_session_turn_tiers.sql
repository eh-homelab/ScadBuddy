-- #2320: the tiers a durable session's current turn runs with, and who sent it. A
-- turn sent through the sessions_* tools carries its sender's tiers (tools/harness.ts
-- `turnPrincipal`); a classic turn holds them in memory, a durable turn's tool
-- activities run in another process, so DurableTurns.send records them with its
-- claim as {"by": {"kind", "id"}, "tiers": [...]}. temporal/toolActivities.ts applies
-- them only while the sender still owns the session (as a resumed classic approval,
-- sessions/manager.ts `resumeTiers`), cut down to what it holds now
-- (auth/tokens.ts `liveTokenTiers`). NULL, or another owner since: the owner's
-- default (auth/principal.ts `harnessPrincipal`), as for every browser turn.
ALTER TABLE ai_sessions ADD COLUMN turn_tiers jsonb;
