-- When the execution writing a native slice's row started it (Task 11 re-review N1):
-- set by each execution's first activity, so the lost-row sweep reads the run's age, and
-- stops a row only if no newer execution has started it since the sweep looked.
ALTER TABLE native_slices ADD COLUMN started_at timestamptz NOT NULL DEFAULT now();
