-- #6574: the shop slice profile names a filament type per material, not a spool.
-- The old shape is {"printer": "<registry id>", "spools": {"<material>": <spool id>}}; the
-- new one is {"printer": "<registry id>", "materials": {"<material>": "<type id>"}}.
-- Since `20261116T0034Z_filament_plan_spool_numbers.sql` a positive spool id here is an
-- SB spool's `number`: its entry becomes that spool's type (`filament_spools.type_id`),
-- whose presets a slot of the material is now sliced with (`filament_spool_type_presets`).
-- A negative id (a loaded tray), a number no spool has, a spool with no type, and an id of
-- any other shape are dropped; the shop sets each such material's type again in Settings
-- → Storefront pricing. Until then a variant of that material has no price (a listing of
-- it is refused at publish, as for a material with no entry). The printer is kept.
UPDATE settings
SET value = jsonb_build_object(
        'printer', value -> 'printer',
        'materials', coalesce((
            SELECT jsonb_object_agg(e.key, to_jsonb(s.type_id::text))
            FROM jsonb_each(value -> 'spools') AS e(key, id)
            JOIN filament_spools s
              ON jsonb_typeof(e.id) IN ('number', 'string')
             AND (e.id #>> '{}') ~ '^[0-9]{1,9}$'
             AND s.number = (e.id #>> '{}')::integer
            WHERE s.type_id IS NOT NULL
        ), '{}'::jsonb)),
    updated_at = now()
WHERE name = 'storefront_slice_profile'
  AND jsonb_typeof(value) = 'object'
  AND jsonb_typeof(value -> 'spools') = 'object'
  AND value ? 'printer';
