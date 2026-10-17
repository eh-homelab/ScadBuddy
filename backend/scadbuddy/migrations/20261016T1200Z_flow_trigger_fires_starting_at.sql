-- #3347: when an admitted fire's attempt began starting its run, under the trigger's
-- row lock. One that began may start its run however long that takes, so it counts
-- toward the rate cap past `ADMITTED_DEAD`; one that never began does not.
ALTER TABLE flow_trigger_fires ADD COLUMN IF NOT EXISTS starting_at timestamptz;
