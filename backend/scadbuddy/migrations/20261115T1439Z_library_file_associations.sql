-- #6395: a native library file associated with a model or with another library file, and
-- how they relate, said from the file's side ("file X is an output_of model Y"). `target`
-- is a print subject's key (`bambuddy/subject.py`), `model:<slug>` or `library:<id>`; a
-- file target also fills `target_file_id`, so purging either file from the trash deletes
-- the row. A trashed file's rows are kept and hidden from every read; a restore shows them
-- again. A deleted model's rows are deleted by its delete (`api/models.py`
-- `delete_template`). `relation` is checked against one list in Python
-- (`print_library/associations.py` `RELATIONS`), so a new relation needs no migration.
CREATE TABLE library_file_associations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    library_file_id bigint NOT NULL REFERENCES library_files (id) ON DELETE CASCADE,
    target text NOT NULL,
    target_file_id bigint REFERENCES library_files (id) ON DELETE CASCADE,
    relation text NOT NULL,
    note text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by jsonb NOT NULL,
    UNIQUE (library_file_id, target, relation),
    CHECK (target ~ '^model:.+$' OR target ~ '^library:[0-9]+$'),
    CHECK ((target_file_id IS NULL) = (target LIKE 'model:%')),
    CHECK (target_file_id IS NULL OR target = 'library:' || target_file_id),
    CHECK (target_file_id IS DISTINCT FROM library_file_id)
);
CREATE INDEX library_file_associations_target ON library_file_associations (target);
CREATE INDEX library_file_associations_target_file ON library_file_associations (target_file_id)
    WHERE target_file_id IS NOT NULL;
