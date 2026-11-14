-- #782 (images in the assistant panel): the images a session's tool results
-- carried, written by src/sessions/manager.ts before it logs the tool.result
-- that names them, and served by GET /api/v1/ai/sessions/:id/blobs/:name
-- (src/sessions/blobs.ts). `name` is the bytes' sha256 and the type's
-- extension; only the four image types the Messages API takes are kept.
CREATE TABLE ai_session_blobs (
  session_id  uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  name        text NOT NULL CHECK (name ~ '^[0-9a-f]{64}\.(png|jpg|gif|webp)$'),
  media_type  text NOT NULL CHECK (media_type IN ('image/png', 'image/jpeg', 'image/gif', 'image/webp')),
  data        bytea NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (session_id, name)
);
