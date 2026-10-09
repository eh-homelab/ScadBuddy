-- #1837: every progress read of an output or library file looks up its newest run
-- (`PrintRunStore.latest_for_output`, `failed_before_queueing`), and the print follow
-- does too. `output_id` holds the run subject for both kinds.
CREATE INDEX IF NOT EXISTS print_runs_subject ON print_runs (output_id, created_at DESC);
