-- #7281 review (spec 2026-11-05-attempt-media-auto-import §4.6): a medium of an archive
-- that the media import refused because it was larger than the upload limit. The import
-- then skips it, and does not count it as missing, while the limit is `limit_bytes` or
-- lower, so it does not download the same file again on each step and each read. A
-- higher limit tries it again. The manual copy ignores this table.
CREATE TABLE order_attempt_media_refusals (
  fulfilment_id text NOT NULL REFERENCES order_item_fulfilments (id) ON DELETE CASCADE,
  archive_id integer NOT NULL,
  archive_media text NOT NULL CHECK (archive_media IN ('finished_photo', 'timelapse')),
  limit_bytes bigint NOT NULL CHECK (limit_bytes > 0),
  refused_at timestamptz NOT NULL,
  PRIMARY KEY (fulfilment_id, archive_id, archive_media)
);
