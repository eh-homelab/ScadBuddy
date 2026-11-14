-- The session transcript is sealed under the session's data key (unified agent
-- runtime, decision D7): `sealed` holds the entry's JSON, `entry` is NULL.
-- `entry` stays for rows written before this migration (Task 3.2 backfills them).
-- `entry_type` is the entry's own `type`, plaintext and not content, so a
-- reader can find a row (warm.ts's last `cost-state`) without opening the rest.
ALTER TABLE ai_session_entries ADD COLUMN sealed bytea;
ALTER TABLE ai_session_entries ADD COLUMN entry_type text;
ALTER TABLE ai_session_entries ALTER COLUMN entry DROP NOT NULL;
