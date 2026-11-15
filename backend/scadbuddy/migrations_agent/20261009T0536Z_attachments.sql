-- #1941: images the assistant panel uploads when the user attaches them
-- (POST /api/v1/ai/attachments, src/attachments/store.ts), before any session
-- may exist. A staging area only: a `user.message` names a row by id, and when
-- its turn starts the bytes move into ai_session_blobs (through
-- SessionBlobs.put, under the same `<sha256>.<ext>` name) and the row is
-- deleted. A row nobody sends expires after an hour and is swept. Rows are per
-- owner and only their owner can read or send them; the bytes are plaintext,
-- as in ai_session_blobs.
CREATE TABLE ai_attachments (
  id                  uuid PRIMARY KEY,
  owner_kind          text NOT NULL,
  owner_id            text NOT NULL,
  name                text NOT NULL CHECK (name ~ '^[0-9a-f]{64}\.(png|jpg|gif|webp)$'),
  media_type          text NOT NULL CHECK (media_type IN ('image/png', 'image/jpeg', 'image/gif', 'image/webp')),
  data                bytea NOT NULL,
  preview_media_type  text NOT NULL CHECK (preview_media_type IN ('image/png', 'image/jpeg', 'image/webp')),
  preview_data        text NOT NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  expires_at          timestamptz NOT NULL
);

CREATE INDEX ai_attachments_owner ON ai_attachments (owner_kind, owner_id);
CREATE INDEX ai_attachments_expires ON ai_attachments (expires_at);
