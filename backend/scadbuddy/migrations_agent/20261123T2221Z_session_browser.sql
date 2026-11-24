-- #7027: a session's browser options (user agent, viewport, locale) for the agent's
-- Playwright MCP. NULL means the browser's defaults. The API validates the bounds
-- (api/crud_agent_sessions.py `SessionBrowser`); the CHECK only keeps the shape an object.
ALTER TABLE ai_sessions ADD COLUMN browser jsonb
  CHECK (browser IS NULL OR jsonb_typeof(browser) = 'object');
