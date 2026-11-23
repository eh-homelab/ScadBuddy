-- #7002 R-7002-6: the newest Bambuddy usage row a pull has read and booked whole, and the
-- Bambuddy instance it was read from. A row of an unlinked Bambuddy spool counts in a
-- pull's `usage_unlinked` only when it is newer than this mark (`created_at`, then `id`).
ALTER TABLE filament_bb_sync_state
    ADD COLUMN usage_read_instance text,
    ADD COLUMN usage_read_at timestamptz,
    ADD COLUMN usage_read_id bigint;
