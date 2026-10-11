-- #2320: the tiers a durable session's current turn runs with. A turn sent through
-- the sessions_* tools carries its sender's tiers (tools/harness.ts `turnPrincipal`);
-- a classic turn holds them in memory, a durable turn's tool activities run in
-- another process, so DurableTurns.send records them with its claim and
-- temporal/toolActivities.ts reads them for each call, cut down to what the owner
-- holds now (auth/tokens.ts `liveTokenTiers`). NULL: the owner's default
-- (auth/principal.ts `harnessPrincipal`), as for every browser turn.
ALTER TABLE ai_sessions ADD COLUMN turn_tiers text[];
