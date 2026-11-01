-- #4719 (spec 2026-10-18 §5.4, §6.1): an attempt the print path starts. `print_run_id` is
-- the `print_runs` row that queued or started it (not a foreign key: a run's row is pruned
-- by its retention, the attempt is kept), `sliced_sha256` and `sliced_blob_key` the
-- accepted quote's sliced file it printed from, held by a `blob_refs` row
-- `(order_item_fulfilment, <id>)` so the file outlives the quote's own hold, and `printer`
-- the registry printer the run named, and `copies` how many the run was sent (the
-- attempt's `quantity` is what it made, set when the print settles). Null on an attempt
-- recorded by hand.
ALTER TABLE order_item_fulfilments
  ADD COLUMN print_run_id text,
  ADD COLUMN sliced_sha256 text,
  ADD COLUMN sliced_blob_key text,
  ADD COLUMN printer text,
  ADD COLUMN copies integer CHECK (copies >= 1);

-- One attempt per run: a print's follow finds its attempt by the run, and a re-sent
-- `order_item_print` that repeats the run records nothing twice.
CREATE UNIQUE INDEX order_item_fulfilments_print_run
  ON order_item_fulfilments (print_run_id) WHERE print_run_id IS NOT NULL;
