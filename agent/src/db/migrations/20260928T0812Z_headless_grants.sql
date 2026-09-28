-- #349: one-shot grants that let the headless browser make ONE outward request
-- (spec §5.3, §8.2). A grant is written only by the
-- `mcp__scadbuddy_browser__authorize_request` tool, which is outward tier and so
-- runs only after a human approved that exact call (method and path) in the
-- UI; it records the approval it ran under. The backend's agent-actor gate
-- (backend/scadbuddy/api/agent_actor.py) uses a grant at most once, and only
-- while the turn that made it is still the session's live turn. See
-- src/harness/headlessGrants.ts.
CREATE TABLE ai_headless_grants (
  id          uuid PRIMARY KEY,
  session_id  uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  -- The turn that ran the approved tool call; the grant dies with it.
  turn_id     uuid NOT NULL,
  -- The approval the tool call ran under (decided, approved and consumed).
  approval_id uuid NOT NULL REFERENCES ai_approvals (id) ON DELETE CASCADE,
  method      text NOT NULL CHECK (method IN ('POST', 'PUT', 'PATCH', 'DELETE')),
  -- The exact request path, e.g. /api/v1/print/outputs/<id>/run; no query.
  path        text NOT NULL CHECK (path ~ '^/api/v1/[A-Za-z0-9._~%/-]+$' AND path !~ '/\.\.?(/|$)'),
  created_at  timestamptz NOT NULL DEFAULT now(),
  expires_at  timestamptz NOT NULL,
  -- Set once the backend let the request through: a grant is used at most once.
  used_at     timestamptz,
  UNIQUE (approval_id)
);
CREATE INDEX ai_headless_grants_lookup ON ai_headless_grants (session_id, method, path) WHERE used_at IS NULL;
