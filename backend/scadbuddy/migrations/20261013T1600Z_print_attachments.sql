-- #309: photos and videos of the real finished object, added to a print in ScadBuddy
-- (plan 2026-09-28-print-history §2.5). Bambuddy keeps the files: a photo in the
-- archive's photos (`archive_photo`, the name Bambuddy gave it) and a copy of each in the
-- print's project `Media/` folder, or the inbox (`library_file_id`). This keeps which
-- files are a print's attachments, and their captions, which Bambuddy has no place for.
CREATE TABLE IF NOT EXISTS print_attachments (
    id bigserial PRIMARY KEY,
    archive_id bigint NOT NULL,
    kind text NOT NULL CHECK (kind IN ('photo', 'video')),
    archive_photo text,
    library_file_id bigint,
    filename text NOT NULL,
    size_bytes bigint NOT NULL,
    caption text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (archive_photo IS NOT NULL OR library_file_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS print_attachments_archive ON print_attachments (archive_id, id);
