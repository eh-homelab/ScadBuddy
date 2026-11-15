-- #4698 review: a run's link always owns an archive over an import made before it.
-- ScadBuddy links its own prints lazily, so an import can come first; the owner order
-- (`print_links._OWNER_ORDER`) now puts a `model:` subject last, and the owner index
-- follows it.
DROP INDEX print_links_owner;
CREATE INDEX print_links_owner
    ON print_links (
        archive_id DESC, (subject LIKE 'model:%'), (subject LIKE 'library:%'), first_seen, subject
    );

-- Whether the import made its model, so a repeat answers what the first answered.
ALTER TABLE print_imports ADD COLUMN created_model boolean NOT NULL DEFAULT false;
