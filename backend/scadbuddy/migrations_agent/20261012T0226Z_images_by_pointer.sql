-- #2479: no image is base64 inside JSON or text. Two places kept one:
--
-- 1. ai_attachments.preview_data held an upload's preview as base64 text. It is
--    bytes now (`preview`), as `data` always was.
-- 2. A `user.turn` event (ai_session_events.event, JSON text) carried each sent
--    image's preview inline, `{mediaType, data, name?}`. A turn now names the
--    image and its preview, `{name, mediaType, preview: {name, mediaType}}`,
--    each one of the session's ai_session_blobs (src/sessions/blobs.ts
--    `sentImageBlobs`). The old turns' previews move into ai_session_blobs under
--    the same `<sha256>.<ext>` name the agent gives them, and the events are
--    rewritten to name them. `name`, when the old turn had it, was the full
--    image's; its type is its extension's.

ALTER TABLE ai_attachments ADD COLUMN preview bytea;
UPDATE ai_attachments SET preview = decode(preview_data, 'base64');
ALTER TABLE ai_attachments ALTER COLUMN preview SET NOT NULL;
ALTER TABLE ai_attachments DROP COLUMN preview_data;

CREATE TEMPORARY TABLE images_by_pointer_previews ON COMMIT DROP AS
SELECT e.session_id, e.seq, x.ord, x.img,
       decode(x.img ->> 'data', 'base64') AS bytes,
       x.img ->> 'mediaType' AS media_type
  FROM ai_session_events e
 CROSS JOIN LATERAL jsonb_array_elements((e.event::jsonb) -> 'images') WITH ORDINALITY AS x(img, ord)
 WHERE strpos(e.event, '"user.turn"') > 0
   AND (e.event::jsonb) ->> 'type' = 'user.turn'
   AND jsonb_typeof((e.event::jsonb) -> 'images') = 'array'
   AND x.img ? 'data';

INSERT INTO ai_session_blobs (session_id, name, media_type, data)
SELECT session_id,
       encode(sha256(bytes), 'hex') || '.' ||
         CASE media_type WHEN 'image/png' THEN 'png' WHEN 'image/jpeg' THEN 'jpg' ELSE 'webp' END,
       media_type, bytes
  FROM images_by_pointer_previews
ON CONFLICT (session_id, name) DO NOTHING;

UPDATE ai_session_events e
   SET event = jsonb_set(e.event::jsonb, '{images}', r.images)::text
  FROM (
    SELECT session_id, seq, jsonb_agg(
             jsonb_build_object(
               'preview', jsonb_build_object(
                 'name', encode(sha256(bytes), 'hex') || '.' ||
                   CASE media_type WHEN 'image/png' THEN 'png' WHEN 'image/jpeg' THEN 'jpg' ELSE 'webp' END,
                 'mediaType', media_type))
             || CASE WHEN img ? 'name' THEN jsonb_build_object(
                  'name', img ->> 'name',
                  'mediaType', CASE substring(img ->> 'name' FROM '\.([a-z]+)$')
                    WHEN 'png' THEN 'image/png' WHEN 'jpg' THEN 'image/jpeg'
                    WHEN 'gif' THEN 'image/gif' ELSE 'image/webp' END)
                ELSE '{}'::jsonb END
             ORDER BY ord) AS images
      FROM images_by_pointer_previews
     GROUP BY session_id, seq
  ) r
 WHERE e.session_id = r.session_id AND e.seq = r.seq;
