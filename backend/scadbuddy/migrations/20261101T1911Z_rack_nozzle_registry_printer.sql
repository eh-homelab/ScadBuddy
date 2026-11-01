-- #4971 (tier 3b, part 2): the rack tables name the registry printer beside the Bambuddy
-- one, so a LAN-only printer's hotends are recorded as seen and its picks name it.
-- Expand only: `printer_id` stays, because the previous build still writes it
-- (`rack/usage.py` `seen`, `record_picks`); `rack_nozzle_seen.printer_id` turns nullable
-- for a printer with no Bambuddy id. Each row a Bambuddy printer the registry links is
-- backfilled. Neither table's key changes: a hotend is its serial, a pick its queue item
-- or its LAN start (#4543). Dropping `printer_id` is the contract step.
ALTER TABLE rack_nozzle_seen
    ALTER COLUMN printer_id DROP NOT NULL,
    ADD COLUMN printer uuid REFERENCES printers (id) ON DELETE SET NULL;
ALTER TABLE rack_nozzle_picks
    ADD COLUMN printer uuid REFERENCES printers (id) ON DELETE SET NULL;

UPDATE rack_nozzle_seen AS s SET printer = p.id
    FROM printers AS p
    WHERE p.bambuddy_printer_id = s.printer_id AND s.printer IS NULL;
UPDATE rack_nozzle_picks AS k SET printer = p.id
    FROM printers AS p
    WHERE p.bambuddy_printer_id = k.printer_id AND k.printer IS NULL;
