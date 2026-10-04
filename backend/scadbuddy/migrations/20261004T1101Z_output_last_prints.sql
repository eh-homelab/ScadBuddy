-- #1060: an output's last print (`library.output_prints`), which the print worker
-- records without the data volume. A row overrides the last-print fields an older
-- meta.json still carries; an output with no row reads its file as before. Outputs are
-- files, so nothing references one: deleting an output deletes its row.
CREATE TABLE output_last_prints (
    output_id     text PRIMARY KEY,
    queue_item_id bigint,
    print_route   text NOT NULL CHECK (print_route IN ('slice_queue')),
    slice_job_id  bigint,
    project_id    bigint,
    plates        jsonb NOT NULL DEFAULT '[]',
    recorded_at   timestamptz NOT NULL DEFAULT now()
);
