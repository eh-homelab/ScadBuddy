-- #3741: each queue item ScadBuddy's reprint of an archive queued (`bambuddy/operations.py`
-- `reprint_run`), so the Prints page marks it as ScadBuddy's
-- (`bambuddy/print_links.py` `_QUEUE_JOBS`). A reprint is not a send: `print_sends` rows
-- are a run's items, which a library file's linking pass, its last run and an order's
-- archives read. `archive_id` is the archive it reprints.
CREATE TABLE IF NOT EXISTS print_reprints (
    queue_item_id bigint PRIMARY KEY,
    subject       text NOT NULL CHECK (subject ~ '^(output:.+|library:[0-9]+)$'),
    archive_id    bigint NOT NULL,
    first_seen    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS print_reprints_subject ON print_reprints (subject);
