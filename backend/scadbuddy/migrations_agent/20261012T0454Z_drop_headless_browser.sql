-- The built-in headless browser (#349) is removed: Microsoft's Playwright MCP is
-- added as an ordinary remote plugin instead (Settings → Plugins), so nothing
-- reads or writes these any more.
--
-- Its switch and its Settings → Plugins tool overrides go first, so a replica
-- still on the old image during the rollout sees the browser as off and never
-- reaches the tables dropped below.
DELETE FROM ai_settings WHERE key IN ('headless_browser_enabled', 'builtin_tools.playwright');

-- The one-shot grants past the backend's agent-actor gate, and the off-origin
-- origins a human approved per session.
DROP TABLE IF EXISTS ai_headless_grants;
DROP TABLE IF EXISTS ai_browser_origins;

-- ai_sessions.durable_turn_id (20261011T1421Z) stays: DurableTurns and the durable
-- sweep still write it, so dropping it would break a replica on the old image.
