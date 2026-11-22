-- A printer's own place is marked `printer_root`. A person's place "beside the printer"
-- (a `printer_id` and no `ams_id` or tray) is not one, so it is never adopted as the root.
ALTER TABLE filament_locations ADD COLUMN printer_root boolean NOT NULL DEFAULT false;

-- One root per printer.
CREATE UNIQUE INDEX filament_locations_printer ON filament_locations (printer_id)
    WHERE printer_root AND archived_at IS NULL;
