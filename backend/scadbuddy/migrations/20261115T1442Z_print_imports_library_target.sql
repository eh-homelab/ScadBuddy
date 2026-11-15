-- #6394: Import print can link an archive to a native library file, not only to a model.
-- `subject` is what the import linked the archive to, the key of its `print_links` row:
-- 'model:<slug>' or 'library:<native file id>'. `slug` is the model's, NULL for a library
-- file's. `library_file_id` and `source` stay where the 3MF was read FROM, not the target.
ALTER TABLE print_imports ADD COLUMN IF NOT EXISTS subject text;
UPDATE print_imports SET subject = 'model:' || slug WHERE subject IS NULL;
ALTER TABLE print_imports ALTER COLUMN subject SET NOT NULL;
ALTER TABLE print_imports ALTER COLUMN slug DROP NOT NULL;
ALTER TABLE print_imports DROP CONSTRAINT IF EXISTS print_imports_subject_check;
ALTER TABLE print_imports ADD CONSTRAINT print_imports_subject_check
    CHECK (
        (subject ~ '^library:[0-9]+$' AND slug IS NULL)
        OR (subject LIKE 'model:%' AND slug IS NOT NULL AND subject = 'model:' || slug)
    );

-- An import's link is its archive's owner last, whatever its subject: a `library:` link
-- that a person imported must never outrank one of ScadBuddy's own prints
-- (`print_links._OWNER_ORDER`). Every `model:` link is an import's, so the order of the
-- rows already there does not change.
DROP INDEX IF EXISTS print_links_owner;
CREATE INDEX print_links_owner
    ON print_links (
        archive_id DESC, (matched_by = 'import'), (subject LIKE 'library:%'), first_seen, subject
    );
