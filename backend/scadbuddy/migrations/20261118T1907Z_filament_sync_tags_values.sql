-- #6265 Decision 4: `filament_sync.tags` takes a direction. #6264's `off` meant "the pull
-- binds Bambuddy's tags, nothing is pushed", which is now `bb_to_sb`; its `sb_to_bb` meant
-- "pull and push", now `two_way`. Each statement is guarded by the old value, so a row
-- with any other value, or no row, stays as it is.
UPDATE settings SET value = jsonb_set(value, '{tags}', '"bb_to_sb"'), updated_at = now()
WHERE name = 'filament_sync' AND value->>'tags' = 'off';
UPDATE settings SET value = jsonb_set(value, '{tags}', '"two_way"'), updated_at = now()
WHERE name = 'filament_sync' AND value->>'tags' = 'sb_to_bb';
