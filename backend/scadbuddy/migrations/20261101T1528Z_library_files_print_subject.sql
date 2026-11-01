-- #4337 (#3818 tier A Task 14): the print subject (`PrintRun.output_id`: an output's id or
-- `library:<file id>`) whose run put this file in the native library, as its laid-out
-- per-print copy or a slice of one. NULL for every other file: a person's upload or copy,
-- an output's filed copy, and a per-print file a person moved or restored, which is then
-- theirs. `print_library.sweep.sweep_print_copies` trashes only marked files nothing keeps.
ALTER TABLE library_files ADD COLUMN IF NOT EXISTS print_subject text;
CREATE INDEX IF NOT EXISTS library_files_print_subject ON library_files (print_subject)
    WHERE print_subject IS NOT NULL AND deleted_at IS NULL;
-- When a run last reused a recorded copy (`ensure_native_copy`): a folder keeps the copy a
-- subject used last, not the one it uploaded last. NULL until reused: its upload's time.
ALTER TABLE output_bambuddy_uploads ADD COLUMN IF NOT EXISTS used_at timestamptz;

-- What runs put here before the column, so the sweep reaches it too (the issue's "how to
-- reach the ones cases 1 and 2 have already orphaned"). Only what a record or a run's
-- result names, never by name or bytes: a person's own file of the same name and bytes is
-- never marked (case 5). Neither a copy that names an output nor a filed one.
-- 1. A recorded copy still in the folder it was recorded in (a moved one is the person's).
UPDATE library_files f SET print_subject = u.output_id
FROM (
    SELECT DISTINCT ON (library_file_id) library_file_id, output_id, folder_id
    FROM output_bambuddy_uploads
    WHERE library_file_id >= 1000000000
    ORDER BY library_file_id, created_at, output_id
) u
WHERE f.id = u.library_file_id
    AND f.folder_id IS NOT DISTINCT FROM u.folder_id
    AND f.output_id IS NULL AND NOT f.filed AND f.print_subject IS NULL;
-- 2. The slices recorded on a copy step 1 marked.
UPDATE library_files f SET print_subject = c.print_subject
FROM output_bambuddy_slices s
JOIN library_files c ON c.id = s.source_library_file_id
WHERE f.id = s.sliced_library_file_id
    AND c.print_subject IS NOT NULL
    AND f.output_id IS NULL AND NOT f.filed AND f.print_subject IS NULL;
-- 3. A copy, and then a slice, a finished run's result names, still in the run's folder,
-- that no record names: its output or model was deleted (cases 1 and 2). Never the file a
-- library print printed as it is (its own subject), and a slice only while the run's copy
-- is still in the run's folder (a moved copy's slices are the person's, with it).
UPDATE library_files f SET print_subject = r.output_id
FROM print_runs r
WHERE r.status = 'succeeded'
    AND f.id = (r.result->>'library_file_id')::bigint
    AND r.output_id <> 'library:' || f.id
    AND f.folder_id IS NOT DISTINCT FROM (r.result->>'folder_id')::bigint
    AND f.output_id IS NULL AND NOT f.filed AND f.print_subject IS NULL
    AND NOT EXISTS (SELECT 1 FROM output_bambuddy_uploads u WHERE u.library_file_id = f.id);
UPDATE library_files f SET print_subject = r.output_id
FROM print_runs r
JOIN library_files c ON c.id = (r.result->>'library_file_id')::bigint
WHERE r.status = 'succeeded'
    AND f.id = (r.result->>'sliced_library_file_id')::bigint
    AND f.id <> c.id
    AND c.folder_id IS NOT DISTINCT FROM (r.result->>'folder_id')::bigint
    AND f.folder_id IS NOT DISTINCT FROM c.folder_id
    AND f.output_id IS NULL AND NOT f.filed AND f.print_subject IS NULL
    AND NOT EXISTS (
        SELECT 1 FROM output_bambuddy_slices s WHERE s.sliced_library_file_id = f.id
    );
