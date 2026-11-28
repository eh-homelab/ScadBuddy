INSERT INTO entity_properties (entity_id, key, value, source, updated_by)
VALUES (%s, %s, %s, 'manual', %s) ON CONFLICT (entity_id, key) DO UPDATE
SET value = EXCLUDED.value, source = 'manual',
updated_at = clock_timestamp(), updated_by = EXCLUDED.updated_by
