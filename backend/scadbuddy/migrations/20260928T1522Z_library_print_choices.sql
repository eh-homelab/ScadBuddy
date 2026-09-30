-- #313: what the print dialog last chose for a file in Bambuddy's library, keyed by
-- Bambuddy's own file id (a library file has no ScadBuddy slug). The same shape as
-- model_print_choices. A file later deleted in Bambuddy leaves its row behind: the
-- dialog can no longer open on it, and a row is a few hundred bytes.
CREATE TABLE library_print_choices (
    file_id    integer PRIMARY KEY,
    choices    jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
