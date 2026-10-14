-- #349: the off-origin origins a human let a session's headless browser open
-- (SCADBUDDY_BROWSER_ALLOWED_ORIGINS). Navigating to an allowed origin that is
-- not the backend's is an outward call that parks for approval the first time
-- in a session; once approved, the origin is recorded here and later
-- navigations to it in the same session run without asking again. See
-- src/harness/browserOrigins.ts.
CREATE TABLE ai_browser_origins (
  session_id  uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  -- A normalised http(s) origin: scheme://host[:port], no path.
  origin      text NOT NULL CHECK (origin ~ '^https?://[^/?#@[:space:]]+$'),
  -- The approval the first navigation ran under (approved and consumed).
  approval_id uuid NOT NULL REFERENCES ai_approvals (id) ON DELETE CASCADE,
  approved_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, origin)
);
