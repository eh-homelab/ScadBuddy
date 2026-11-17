-- The preset Bambuddy sent last for a linked spool (#6262 Task 7 re-review, ruling R1):
-- its `slicer_filament` and name. The pull rewrites or deletes only the SB preset rows
-- that still hold these values; a row a person or the native migration set is never
-- touched (`filament/bambuddy_pull.py` `_bb_presets`).
ALTER TABLE filament_bb_links
    ADD COLUMN bb_slicer_filament text,
    ADD COLUMN bb_slicer_filament_name text;
