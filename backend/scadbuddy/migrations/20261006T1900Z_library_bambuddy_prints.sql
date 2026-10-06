-- #976: the prints of Bambuddy library files ScadBuddy queued (`bambuddy.print_links`).
-- A library-file run has no output, so `output_bambuddy_prints` cannot hold it. One row
-- per queue item, written when the run queues it; `archive_id` is filled in once
-- Bambuddy dispatches the item and names its archive. `gone` marks an item Bambuddy
-- dropped before it named one, so it is not read again.
CREATE TABLE library_bambuddy_prints (
    queue_item_id   bigint PRIMARY KEY,
    library_file_id bigint NOT NULL,
    plate_id        integer,
    printer_id      bigint,
    archive_id      bigint,
    -- The file's name as the queue item reported it, for an archive Bambuddy deletes.
    name            text,
    gone            boolean NOT NULL DEFAULT false,
    first_seen      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX library_bambuddy_prints_archive
    ON library_bambuddy_prints (archive_id DESC, first_seen) WHERE archive_id IS NOT NULL;
CREATE INDEX library_bambuddy_prints_pending
    ON library_bambuddy_prints (first_seen) WHERE archive_id IS NULL AND NOT gone;
