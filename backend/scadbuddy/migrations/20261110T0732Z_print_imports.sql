-- #4698: "Import print". A person adopts a Bambuddy archive that ScadBuddy did not start
-- and links it to a model. The link is a `print_links` row whose subject is the model,
-- 'model:<slug>' (`bambuddy.subject.PrintSubject.model`), found by `matched_by` 'import'.
-- A reprint of such an archive records its queue item under the same subject.
ALTER TABLE print_links DROP CONSTRAINT IF EXISTS print_links_subject_check;
ALTER TABLE print_links ADD CONSTRAINT print_links_subject_check
    CHECK (subject ~ '^(output:.+|library:[0-9]+|model:.+)$');
ALTER TABLE print_links DROP CONSTRAINT IF EXISTS print_links_matched_by_check;
ALTER TABLE print_links ADD CONSTRAINT print_links_matched_by_check
    CHECK (matched_by IN ('queue_item', 'content_hash', 'import'));
ALTER TABLE print_reprints DROP CONSTRAINT IF EXISTS print_reprints_subject_check;
ALTER TABLE print_reprints ADD CONSTRAINT print_reprints_subject_check
    CHECK (subject ~ '^(output:.+|library:[0-9]+|model:.+)$');

-- What each import read (`bambuddy/print_import.py`): one row per archive, written in the
-- same transaction as its `print_links` row. `source` is where the person chose the 3MF
-- from: the archive's sliced file, the archive's slicer project, a Bambuddy library file,
-- or the printer's storage. `library_file_id` is the library file chosen, or the library
-- copy a printer file was kept as; `printer_path` the file's path on the printer.
CREATE TABLE IF NOT EXISTS print_imports (
    archive_id      bigint PRIMARY KEY,
    slug            text NOT NULL,
    source          text NOT NULL
        CHECK (source IN ('archive', 'archive_source', 'library', 'printer')),
    library_file_id bigint,
    printer_id      bigint,
    printer_path    text,
    filename        text NOT NULL,
    size            bigint,
    sha256          text,
    imported_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS print_imports_slug ON print_imports (slug);
