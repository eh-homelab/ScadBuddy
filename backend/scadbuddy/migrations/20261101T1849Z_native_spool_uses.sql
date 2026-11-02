-- What each LAN start of a native print uses of ScadBuddy's own spools (#4643 part 2):
-- one row per start and slot, saved as the printer takes the start (`lan_print`), with
-- the grams the sliced plate gives the slot and the native record the overlay put in its
-- tray. When the run's print settles (the follow's read, `spools/spool_providers.py`
-- `native_use_hook`) every row of the run is settled once: `applied` when its start
-- printed to the end, and its grams taken off the spool's `remaining_g`; a failed,
-- cancelled or unknown start takes nothing. A spool deleted meanwhile takes its rows.
CREATE TABLE native_spool_uses (
    run_id text NOT NULL,
    subtask_name text NOT NULL,
    slot_id integer NOT NULL,
    spool_id uuid NOT NULL REFERENCES native_spools (id) ON DELETE CASCADE,
    grams double precision NOT NULL CHECK (grams >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    settled_at timestamptz,
    applied boolean,
    PRIMARY KEY (run_id, subtask_name, slot_id)
);
CREATE INDEX native_spool_uses_pending ON native_spool_uses (run_id) WHERE settled_at IS NULL;
CREATE INDEX native_spool_uses_spool ON native_spool_uses (spool_id);
