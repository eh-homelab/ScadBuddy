-- The outputs cut over from NFS, release B (#7257, outputs-in-Postgres spec 2026-11-23
-- §4.5 phase 3; plan 2026-11-25 D2, D5). This release reads no output file, so it runs
-- only once release A wrote the cut-over mark: every output's record is a row and its
-- bytes are blobs. It raises otherwise, which stops the API's start; the pods of the
-- release before keep serving. A database that never held an output (a new install, a
-- test schema) needs no mark. On a `bambuddy` store the mark is never written (outputs
-- never go into Bambuddy's library, spec §6.4), so this refuses such an install that
-- holds an output, and the text names the remedy.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM output_cutover)
        AND (EXISTS (SELECT 1 FROM outputs) OR EXISTS (SELECT 1 FROM output_index)) THEN
        RAISE EXCEPTION 'the outputs are not cut over (#7257): the copies have not finished, or the store is bambuddy, where outputs are never copied (spec 6.4). Move the store to s3 or local, run release 3a until GET /api/v1/outputs/copy-status names cut_over_at, then upgrade';
    END IF;
    IF EXISTS (SELECT 1 FROM outputs WHERE bytes_at IS NULL) THEN
        RAISE EXCEPTION 'outputs whose bytes are only in files remain (#7257): read GET /api/v1/outputs/copy-status';
    END IF;
END $$;

-- The list's text of the rows that a pod of an older build saved during release A's
-- roll, from their index rows (same id and slug, as release A's fill); none is ''.
UPDATE outputs o SET params_text = x.params_text
FROM output_index x
WHERE x.output_id = o.output_id AND x.slug = o.slug AND o.params_text IS NULL;
UPDATE outputs SET params_text = '' WHERE params_text IS NULL;
ALTER TABLE outputs
    ALTER COLUMN params_text SET DEFAULT '',
    ALTER COLUMN params_text SET NOT NULL;

-- `outputs` is the list's only table now, and the hold reaper reads rows only. Release A
-- pods skip a write to either table once it is gone (`tolerate_dropped`).
DROP TABLE output_index;
DROP TABLE output_hold_slugs;
