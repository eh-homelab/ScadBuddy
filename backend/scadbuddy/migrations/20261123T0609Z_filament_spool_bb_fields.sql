-- #7149 (plan docs/superpowers/plans/2026-11-23-spool-bb-fields.md): three spool fields
-- Bambuddy keeps, and the consumed-counter reset.
-- R-7149-1: how the spool's data came in (Bambuddy's `data_origin`, its String(20)).
-- R-7149-2: whether the spool was added full; null is not known.
-- R-7149-3: when its tag was written (Bambuddy's `encode_time`).
ALTER TABLE filament_spools
    ADD COLUMN data_origin text CHECK (data_origin ~ '^[a-z0-9_]{1,20}$'),
    ADD COLUMN added_full boolean,
    ADD COLUMN tag_encoded_at timestamptz;

-- R-7149-4: `consumed_reset` starts the spool's consumed counter again; it has no grams.
ALTER TABLE filament_spool_events
    DROP CONSTRAINT filament_spool_events_kind_check,
    ADD CONSTRAINT filament_spool_events_kind_check CHECK (kind IN (
        'created', 'loaded', 'unloaded', 'moved', 'print_started', 'print_ended', 'weighed',
        'ams_reading', 'dried', 'edited', 'tag_bound', 'tag_unbound', 'archived', 'restored',
        'synced_from_bb', 'consumed_reset'
    ));
