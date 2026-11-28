INSERT INTO entities (kind, ref) VALUES (%s, %s)
ON CONFLICT (kind, ref) DO UPDATE SET kind = EXCLUDED.kind RETURNING id
