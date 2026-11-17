-- #6574: the shop slice profile names a filament preset per material, not a spool.
-- The old shape is {"printer": "<registry id>", "spools": {"<material>": <spool id>}}; the
-- new one is {"printer": "<registry id>", "materials": {"<material>": {"source", "id"}}}.
-- A spool id here is a loaded tray's negative id (or a Bambuddy spool's), and the preset
-- it slices with is chosen from Bambu Studio's catalogue in Python
-- (`bambuddy/resolver.py` `_by_filament_id`, by the tray's `tray_info_idx`). No table
-- holds that catalogue, so SQL cannot name the preset: each material's entry is dropped,
-- the printer is kept, and the shop sets each material's preset again in Settings →
-- Storefront pricing. Until then a variant of that material has no price (a listing of it
-- is refused at publish, as for a material with no entry).
UPDATE settings
SET value = jsonb_build_object('printer', value -> 'printer', 'materials', '{}'::jsonb),
    updated_at = now()
WHERE name = 'storefront_slice_profile'
  AND jsonb_typeof(value) = 'object'
  AND value ? 'spools'
  AND value ? 'printer';
