-- Filament capture (#6263): three changes to `filament_spool_events`.
--
-- 1. `filament_spool_events_job` finds a job's print estimates by `data.job`
--    (`ledger.job_of`, the open-estimate lookup). The predicate is the same as the
--    lookup's: `print_started` events of ref kind `print_run` (`ledger.PRINT_RUN`).
-- 2. Estimates written before #6263 carry the job only in their `ref_id`
--    (`run/subtask/slot`). The subtask is copied into `data.job`, exactly as
--    `ledger.job_of` reads it (the middle of the ref, so a subtask with a `/` stays
--    whole). A row that has a string `data.job`, or a `ref_id` that is not that form
--    (fewer than three parts), is left alone.
-- 3. A Bambuddy weighing (ref kind `bb_scale`) is a scale reading: its source changes
--    from `bb` to `scale`. This is a label change only. No `grams_left` changes,
--    because the walk gives the same anchor for a weighing labelled `bb` or `scale`;
--    `last_weighed_*` and the unique index (which does not include the source) stay
--    as they are.
-- Each statement is guarded, so a second run changes nothing.

CREATE INDEX IF NOT EXISTS filament_spool_events_job
    ON filament_spool_events ((data ->> 'job'))
    WHERE kind = 'print_started' AND ref_kind = 'print_run'; -- ledger.PRINT_RUN

UPDATE filament_spool_events
SET data = data || jsonb_build_object(
    'job', regexp_replace(ref_id, '^[^/]*/(.*)/[^/]*$', '\1')
)
WHERE kind = 'print_started'
  AND ref_kind = 'print_run' -- ledger.PRINT_RUN
  AND jsonb_typeof(data -> 'job') IS DISTINCT FROM 'string'
  AND ref_id ~ '^[^/]*/.*/[^/]*$';

UPDATE filament_spool_events
SET source = 'scale'
WHERE kind = 'weighed'
  AND ref_kind = 'bb_scale' -- the Bambuddy weighing's ref kind
  AND source = 'bb';
