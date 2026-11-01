-- #4644: an output's last print when it printed over the LAN (spec 2026-10-16 §6.3). A
-- LAN start has no queue item or slice job, so the row names the run that started it,
-- the registry printer and each plate with its copies (`library/output_prints.py`
-- `record_lan`, from `print_succeed`); the native follow moves `print_stage` and
-- `print_settled` as it reads the print (`printers/progress.py` `lan_last_print`). A
-- Bambuddy print's row leaves them null, empty and false.
ALTER TABLE output_last_prints DROP CONSTRAINT output_last_prints_print_route_check;
ALTER TABLE output_last_prints ADD CONSTRAINT output_last_prints_print_route_check
    CHECK (print_route IN ('slice_queue', 'lan'));
ALTER TABLE output_last_prints
    ADD COLUMN print_run_id  text,
    -- Not a foreign key, as `print_outcomes.printer`: the record outlives the printer.
    ADD COLUMN printer       uuid,
    ADD COLUMN lan_plates    jsonb   NOT NULL DEFAULT '[]',
    ADD COLUMN print_stage   text,
    ADD COLUMN print_settled boolean NOT NULL DEFAULT false;

-- #4543: a native (LAN) rack print's picks. A LAN start has no Bambuddy queue item and
-- no archive, so its rows are keyed by the run and the start's `subtask_name` (one per
-- plate copy) instead, and settled by the native follow from the printer's report. A
-- row has one key or the other. `printer_id` stays the Bambuddy printer the rack tables
-- key on (re-keying them is #4971): null for a LAN-only printer.
ALTER TABLE rack_nozzle_picks DROP CONSTRAINT rack_nozzle_picks_pkey;
ALTER TABLE rack_nozzle_picks
    ALTER COLUMN queue_item_id DROP NOT NULL,
    ALTER COLUMN printer_id DROP NOT NULL,
    ADD COLUMN lan_run_id  text,
    ADD COLUMN lan_subtask text,
    ADD CONSTRAINT rack_nozzle_picks_queue_item UNIQUE (queue_item_id, group_id),
    ADD CONSTRAINT rack_nozzle_picks_lan_start UNIQUE (lan_run_id, lan_subtask, group_id),
    ADD CONSTRAINT rack_nozzle_picks_one_key CHECK (
        (queue_item_id IS NULL) = (lan_run_id IS NOT NULL)
        AND (lan_run_id IS NULL) = (lan_subtask IS NULL)
    );

ALTER TABLE rack_nozzle_pick_spools DROP CONSTRAINT rack_nozzle_pick_spools_pkey;
ALTER TABLE rack_nozzle_pick_spools
    ALTER COLUMN queue_item_id DROP NOT NULL,
    ADD COLUMN lan_run_id  text,
    ADD COLUMN lan_subtask text,
    ADD CONSTRAINT rack_nozzle_pick_spools_queue_item UNIQUE (queue_item_id, group_id, slot_id),
    ADD CONSTRAINT rack_nozzle_pick_spools_lan_start
        UNIQUE (lan_run_id, lan_subtask, group_id, slot_id),
    ADD CONSTRAINT rack_nozzle_pick_spools_one_key CHECK (
        (queue_item_id IS NULL) = (lan_run_id IS NOT NULL)
        AND (lan_run_id IS NULL) = (lan_subtask IS NULL)
    );

-- A LAN print has no archive: its row is keyed by its start, as its pick is.
ALTER TABLE rack_nozzle_prints DROP CONSTRAINT rack_nozzle_prints_pkey;
ALTER TABLE rack_nozzle_prints
    ALTER COLUMN archive_id DROP NOT NULL,
    ADD COLUMN lan_run_id  text,
    ADD COLUMN lan_subtask text,
    ADD CONSTRAINT rack_nozzle_prints_archive UNIQUE (archive_id, group_id),
    ADD CONSTRAINT rack_nozzle_prints_lan_start UNIQUE (lan_run_id, lan_subtask, group_id),
    ADD CONSTRAINT rack_nozzle_prints_one_key CHECK (
        (archive_id IS NULL) = (lan_run_id IS NOT NULL)
        AND (lan_run_id IS NULL) = (lan_subtask IS NULL)
    );
