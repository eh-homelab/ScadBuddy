-- One place per printer itself (no AMS unit, no tray). `filament_locations_tray` treats a
-- NULL ams_id or tray_id as distinct, so it never held two of these, and
-- `filament_locations_ams_unit` needs an ams_id.
CREATE UNIQUE INDEX filament_locations_printer ON filament_locations (printer_id)
    WHERE ams_id IS NULL AND tray_id IS NULL AND printer_id IS NOT NULL
      AND archived_at IS NULL;
