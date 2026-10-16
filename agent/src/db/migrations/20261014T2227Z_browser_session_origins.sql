-- #982: the origins the user let a session's headless browser (the Playwright MCP
-- plugin) open, beyond Settings' allow list (src/browser/store.ts). `session`: approved
-- "for this chat", so later navigations there run without asking. `once`: one
-- navigation there was approved, so the pages the browser shows there may be read, but
-- the next navigation to it asks again. Kept with the session.
CREATE TABLE ai_browser_session_origins (
  session_id  uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  -- A normalised http(s) origin: scheme://host[:port], no path.
  origin      text NOT NULL CHECK (origin ~ '^https?://[^/?#@[:space:]]+$'),
  scope       text NOT NULL CHECK (scope IN ('once', 'session')),
  approved_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, origin)
);
CREATE INDEX ai_browser_session_origins_recent ON ai_browser_session_origins (approved_at DESC);
