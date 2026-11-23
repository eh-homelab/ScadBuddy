-- #7231 (spec 2026-11-05-attempt-media-auto-import §4.2, §4.3, §4.6): an attachment
-- copied from a Bambuddy archive names that archive and the medium. Both are null for any
-- other file, or both are set. The partial unique index stops a second row for one copy:
-- two imports, or an import and the manual copy, that race. It includes `filename`, so a
-- person can still copy a different finish photo of the same archive.
ALTER TABLE entity_attachments
  ADD COLUMN archive_id integer,
  ADD COLUMN archive_media text CHECK (archive_media IN ('finished_photo', 'timelapse')),
  ADD CONSTRAINT entity_attachments_archive_pair
    CHECK ((archive_id IS NULL) = (archive_media IS NULL));

-- The copies `copy_in` wrote before: on an attempt (since #5268) or on its item (before).
-- Their caption names the archive, and their name the medium. A caption a person edited,
-- or another name, stays null. Of two rows on one key, only the oldest is set.
WITH copies AS (
  SELECT id, subject, filename, created_at,
         substring(caption FROM '^Attempt [0-9]+, Bambuddy archive ([0-9]+)$')::integer
           AS archive_id,
         CASE
           WHEN filename LIKE 'finish\_%' THEN 'finished_photo'
           WHEN filename LIKE 'timelapse-%' THEN 'timelapse'
         END AS archive_media
  FROM entity_attachments
  WHERE (subject LIKE 'order\_item\_fulfilment:%' OR subject LIKE 'order\_item:%')
    AND caption ~ '^Attempt [0-9]+, Bambuddy archive [0-9]+$'
),
oldest AS (
  SELECT id, archive_id, archive_media,
         row_number() OVER (
           PARTITION BY subject, archive_id, archive_media, filename
           ORDER BY created_at, id
         ) AS n
  FROM copies
  WHERE archive_media IS NOT NULL
)
UPDATE entity_attachments a
SET archive_id = o.archive_id, archive_media = o.archive_media
FROM oldest o
WHERE a.id = o.id AND o.n = 1;

CREATE UNIQUE INDEX entity_attachments_archive_copy
  ON entity_attachments (subject, archive_id, archive_media, filename)
  WHERE archive_id IS NOT NULL;

-- When the import last asked Bambuddy for the attempt's media, and when it last asked
-- Bambuddy to scan the printer for its timelapse.
ALTER TABLE order_item_fulfilments
  ADD COLUMN timelapse_scanned_at timestamptz,
  ADD COLUMN media_checked_at timestamptz;
