-- #7453 (spec 2026-11-05-attempt-media-auto-import §4.6): the media sweep.
--
-- A medium that an archive did not offer once `FRESH` (an hour) had passed since its
-- print completed: most often the timelapse of a printer with timelapse off. Each media
-- step rewrites the attempt's rows from what it read, as `mirror_attempt`'s `missing`
-- reads it, and the sweep leaves these media out, so such an attempt is not read again
-- every hour for its 14 days. A step that finds the medium offered deletes the row. The
-- triggers (a hand attempt, a heard outcome, a read of the archives) ignore this table.
CREATE TABLE order_attempt_media_not_offered (
  fulfilment_id text NOT NULL REFERENCES order_item_fulfilments (id) ON DELETE CASCADE,
  archive_id integer NOT NULL,
  archive_media text NOT NULL CHECK (archive_media IN ('finished_photo', 'timelapse')),
  seen_at timestamptz NOT NULL,
  PRIMARY KEY (fulfilment_id, archive_id, archive_media)
);

-- The sweep (`OrderStore.attempts_missing_media`) reads the attempts of the last 14 days
-- every 300 s. Only an attempt that names an archive or a run can qualify; the query
-- repeats this predicate so the planner can use the index.
CREATE INDEX order_item_fulfilments_media_sweep
  ON order_item_fulfilments (created_at)
  WHERE archive_ids <> '{}' OR print_run_id IS NOT NULL;
