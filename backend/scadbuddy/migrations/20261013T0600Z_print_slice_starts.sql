-- #2715: every slice job a print run starts, and every start that got no answer.
--
-- A start whose POST got no answer (or a 5xx or 429) is started again (#2081), but
-- Bambuddy may have taken the first one: that job then slices to a file nothing
-- queues. The run cannot know its id. Bambuddy numbers jobs in sequence, so the
-- housekeeping sweep looks beside the jobs recorded after the lost start for one that
-- slices the same copy, that no run and no background slice recorded, and removes the
-- file it sliced to (bambuddy/lost_slices.py). `job_id` is NULL for a lost start.
CREATE TABLE IF NOT EXISTS print_slice_starts (
    id bigserial PRIMARY KEY,
    library_file_id integer NOT NULL,
    job_id integer,
    -- A lost start the sweep has finished with.
    swept boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS print_slice_starts_by_job
    ON print_slice_starts (job_id, created_at)
    WHERE job_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS print_slice_starts_lost
    ON print_slice_starts (created_at)
    WHERE job_id IS NULL AND NOT swept;
