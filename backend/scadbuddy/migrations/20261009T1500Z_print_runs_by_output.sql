-- #1837: an output's (or a library file's, `library:<id>`) newest run, which every
-- /progress poll and every follow pass reads (`latest_for_output`), and which the output
-- listing reads for a page of outputs at once (`failed_before_queueing`).
CREATE INDEX IF NOT EXISTS print_runs_output ON print_runs (output_id, created_at DESC);
