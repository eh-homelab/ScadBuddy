-- #1754: what the print dialog last chose is remembered in one store,
-- model_print_choices, keyed by the print's options scope: a model's slug, or a
-- Bambuddy library file's subject key 'library:<file id>' (a slug never holds ':').
-- Every library_print_choices row is copied in under its key, with its time; where the
-- key is already present the newer row wins, so applying this again changes nothing.
-- library_print_choices is no longer read or written, but is kept with its rows so an
-- image rolled back past #1754 still finds it; a later migration may drop it.
INSERT INTO model_print_choices (model_id, choices, updated_at)
SELECT 'library:' || file_id, choices, updated_at
FROM library_print_choices
ON CONFLICT (model_id) DO UPDATE
SET choices = EXCLUDED.choices, updated_at = EXCLUDED.updated_at
WHERE model_print_choices.updated_at < EXCLUDED.updated_at;
