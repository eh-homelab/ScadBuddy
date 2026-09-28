-- #268: when each output's last print was started, so the print watcher finds the
-- prints to follow again after a restart (`bambuddy/watcher.py` `PgPrintLog`).
CREATE TABLE print_watches (
    output_id  text PRIMARY KEY,
    printed_at timestamptz NOT NULL
);
CREATE INDEX print_watches_printed_at ON print_watches (printed_at);
