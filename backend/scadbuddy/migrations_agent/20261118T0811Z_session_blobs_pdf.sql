-- #6267: a session's first message can carry an invoice PDF. The agent stores it in
-- ai_session_blobs under `<sha256>.pdf`, as it stores an image, and reads it back by name
-- for the turn, which sends it to the model as a document block (src/sessions/images.ts
-- `userPrompt`). Both CHECKs gain the PDF; no row changes. The staged `ai_attachments`
-- stay image-only: an import session's PDF comes from the API, not from the panel.
ALTER TABLE ai_session_blobs DROP CONSTRAINT ai_session_blobs_name_check;
ALTER TABLE ai_session_blobs ADD CONSTRAINT ai_session_blobs_name_check
  CHECK (name ~ '^[0-9a-f]{64}\.(png|jpg|gif|webp|pdf)$');
ALTER TABLE ai_session_blobs DROP CONSTRAINT ai_session_blobs_media_type_check;
ALTER TABLE ai_session_blobs ADD CONSTRAINT ai_session_blobs_media_type_check
  CHECK (media_type IN ('image/png', 'image/jpeg', 'image/gif', 'image/webp', 'application/pdf'));
