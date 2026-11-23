-- The Bambuddy a preset's `bb_preset_id` belongs to (#7176, review of PR #7201).
-- `bb_instance` is `filament/bb_sync_nudge.py` `instance_for` of the Bambuddy URL the id
-- was read from or sent to: the mirror rewrites a preset in place (`PUT`) only on that
-- Bambuddy, never on another one at a new URL. NULL is a row written before this file,
-- which the mirror takes for the configured Bambuddy's. The rows that have an id take the
-- filament sync's recorded instance, when one is recorded.
--
-- `mirror_create_floor` is the highest Bambuddy local preset id the mirror listed before
-- it sent a `POST` whose answer it has not stored yet. The next send first looks for a
-- preset of the same name above it, so a create whose answer was lost is not made twice.
ALTER TABLE filament_presets
    ADD COLUMN bb_instance text,
    ADD COLUMN mirror_create_floor bigint;

UPDATE filament_presets
SET bb_instance = (SELECT instance FROM filament_bb_sync_state LIMIT 1)
WHERE bb_preset_id IS NOT NULL;
