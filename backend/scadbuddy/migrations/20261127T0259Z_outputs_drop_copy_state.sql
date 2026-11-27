-- The retired copies' state goes (#7887; plan 2026-11-25 D11). Release B (#7257) stopped
-- the copies of the outputs' files to rows and to blobs, and `GET /outputs/copy-status`,
-- their last reader, is removed with this file. The release before this one reads none
-- of these, and no code writes them.
-- `output_cutover` names a byte run (`byte_run_id`), so it goes before `output_byte_runs`.
-- Release B's migration (20261125T1737Z) reads `output_cutover`; it sorts before this
-- file, so it has run wherever this one runs.
DROP TABLE output_cutover;
DROP TABLE output_byte_runs;
DROP TABLE output_copy_runs;

ALTER TABLE outputs
    DROP COLUMN copied_at,
    DROP COLUMN files_stamp,
    DROP COLUMN bytes_stamp,
    DROP COLUMN damaged;
