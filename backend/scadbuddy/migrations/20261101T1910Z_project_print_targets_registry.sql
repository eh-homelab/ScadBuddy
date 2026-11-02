-- #4971 (tier 3b, part 2): the printer a Bambuddy project last printed on, by registry id
-- when it has no Bambuddy id (a LAN-only printer, whose run records `printer_id` 0).
-- Expand only: `printer_id` stays NOT NULL, since the previous build reads it as an
-- integer and upserts it without this column, so `printer` is read only beside a 0
-- (`bambuddy/uploads.py`). Nothing to backfill: a row with a Bambuddy id keeps using it,
-- and a 0 already written names no printer. Re-keying the column is the contract step.
ALTER TABLE project_print_targets
    ADD COLUMN printer uuid REFERENCES printers (id) ON DELETE SET NULL;
