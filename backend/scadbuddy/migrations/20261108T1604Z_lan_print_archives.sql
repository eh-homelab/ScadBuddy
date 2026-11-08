-- #5661: the archive a LAN start became. A LAN start has no queue item and no send, so a
-- link found by content hash names no run. Bambuddy archives the print under the file
-- name the run uploaded, which names the start (`workflows/printing.py` `_start_lan`).
-- The API records the start here the first time it reads the archive
-- (`api/print_history.py` `_part_settings`), and follows it on each later read. One
-- start per archive, and the first one recorded is kept. Not a foreign key, as
-- `print_outcomes`: the run's row is pruned by its retention.
CREATE TABLE lan_print_archives (
    archive_id   bigint PRIMARY KEY,
    run_id       text NOT NULL,
    subtask_name text NOT NULL,
    plate_id     integer,
    first_seen   timestamptz NOT NULL DEFAULT now()
);
