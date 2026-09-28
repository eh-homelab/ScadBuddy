-- #306: the Bambuddy archives an output's prints produced (`bambuddy.print_links`).
-- Keyed by output and archive: an archive can be reached from more than one queue
-- item, and one sliced file printed several times has several archives. Outputs are
-- still files, so nothing here references one: deleting an output deletes its rows.
CREATE TABLE output_bambuddy_prints (
    output_id     text NOT NULL,
    archive_id    bigint NOT NULL,
    queue_item_id bigint,
    plate_id      integer,
    printer_id    bigint,
    matched_by    text NOT NULL CHECK (matched_by IN ('queue_item', 'content_hash')),
    first_seen    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (output_id, archive_id)
);
CREATE INDEX output_bambuddy_prints_archive ON output_bambuddy_prints (archive_id);
-- The SHA-256 Bambuddy reports for a sliced file: the archive of a print of it has
-- the same `content_hash`, which is how a print is found once its queue item is gone.
ALTER TABLE output_bambuddy_slices ADD COLUMN file_hash text;
CREATE INDEX output_bambuddy_slices_hash ON output_bambuddy_slices (file_hash)
    WHERE file_hash IS NOT NULL;
