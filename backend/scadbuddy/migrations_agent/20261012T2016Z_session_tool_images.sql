-- #2478: which of a durable session's ai_session_blobs each tool call produced, in
-- the order its result held them. The agent-tools activity writes them as it keeps
-- the images (src/sessions/blobs.ts `SessionBlobs.put` with the call's tool_use_id);
-- agent-durable's follow_session reads them to name the images on that call's
-- `tool.result` event, `{name, mediaType}`, so the panel loads them from the session
-- blob route. A link table, not a column on ai_session_blobs: one image (one row
-- there, by its sha256) may come from several calls.
CREATE TABLE ai_session_tool_images (
  session_id   uuid NOT NULL,
  tool_use_id  text NOT NULL CHECK (length(tool_use_id) BETWEEN 1 AND 256),
  ord          smallint NOT NULL CHECK (ord >= 0),
  name         text NOT NULL,
  PRIMARY KEY (session_id, tool_use_id, ord),
  FOREIGN KEY (session_id, name) REFERENCES ai_session_blobs (session_id, name) ON DELETE CASCADE
);
