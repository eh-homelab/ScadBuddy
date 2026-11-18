-- #6265 Decision 4: `filament_sync.tags` takes a direction. #6264's `off` meant "the pull
-- binds Bambuddy's tags, nothing is pushed", which is now `bb_to_sb`; its `sb_to_bb` meant
-- "pull and push", now `two_way`. Each statement is guarded by the old value, so a row
-- with any other value, or no row, stays as it is.
--
-- Before #6265 a `spools` of `off` skipped the whole pull, and the tag push ran only on
-- `tags` `sb_to_bb`. So such a row pulls nothing after: `weights`, `presets` and `tags`
-- are `off`, and a `tags` `sb_to_bb` stays the push it was. The two `tags` rewrites after
-- it leave such a row alone. A second run changes nothing.
WITH wanted AS (
    SELECT name, jsonb_build_object(
        'weights', 'off',
        'presets', 'off',
        'tags', CASE WHEN value->>'tags' = 'sb_to_bb' THEN 'sb_to_bb' ELSE 'off' END
    ) AS off
    FROM settings WHERE name = 'filament_sync' AND value->>'spools' = 'off'
)
UPDATE settings s SET value = s.value || w.off, updated_at = now()
FROM wanted w WHERE s.name = w.name AND NOT s.value @> w.off;
UPDATE settings SET value = jsonb_set(value, '{tags}', '"bb_to_sb"'), updated_at = now()
WHERE name = 'filament_sync' AND value->>'tags' = 'off'
  AND value->>'spools' IS DISTINCT FROM 'off';
UPDATE settings SET value = jsonb_set(value, '{tags}', '"two_way"'), updated_at = now()
WHERE name = 'filament_sync' AND value->>'tags' = 'sb_to_bb'
  AND value->>'spools' IS DISTINCT FROM 'off';
