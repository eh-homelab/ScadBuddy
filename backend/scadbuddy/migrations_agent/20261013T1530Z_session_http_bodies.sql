-- #2374: the bodies http_request saved because they were too long to return inline
-- (src/sessions/httpBodies.ts). They were files in the session's directory on the pod
-- that made the request, an emptyDir: another replica, or the same one after a deploy,
-- answered 404 to AI activity's "Download body" and to the agent's own
-- http_response_read. Kept with the session (ON DELETE CASCADE), the newest
-- MAX_SAVED (10) per session; `id` is the UUID the audit row names.
CREATE TABLE ai_session_http_bodies (
  session_id    uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  id            uuid NOT NULL,
  url           text NOT NULL,
  content_type  text,
  size_bytes    integer NOT NULL CHECK (size_bytes >= 0),
  complete      boolean NOT NULL,
  data          bytea NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (session_id, id)
);
CREATE INDEX ai_session_http_bodies_age ON ai_session_http_bodies (session_id, created_at DESC);
