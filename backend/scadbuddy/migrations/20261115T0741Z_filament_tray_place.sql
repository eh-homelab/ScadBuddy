-- Which SB spool is in each AMS tray (#6262 Task 4, spec
-- 2026-11-14-filament-management-design.md §5.3 "current tray"). A spool's tray place is
-- its `location_id` when that place has `printer_id` set. `tray_fingerprint` is what the
-- tray showed when a person placed the spool there (`filament/inventory.py`
-- `tray_fingerprint`: `MATERIAL|#RRGGBBAA`, or `MATERIAL|` for no colour), so an untagged
-- tray holds the spool only while it still shows that. A move through
-- `filament/locations.py` `move`/`move_on` clears it; a printer merge, which keeps the
-- tray, keeps it. Derived and stored; no row is rewritten here.
ALTER TABLE filament_spools ADD COLUMN IF NOT EXISTS tray_fingerprint text;
