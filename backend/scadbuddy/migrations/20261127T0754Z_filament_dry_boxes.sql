-- #7167: a spool's home dry box, when it was last asked for, and the `non_print_waste` event.
ALTER TABLE filament_spools
    ADD COLUMN home_location_id uuid REFERENCES filament_locations (id) ON DELETE SET NULL,
    ADD COLUMN home_asked_at timestamptz;

ALTER TABLE filament_spool_events
    DROP CONSTRAINT filament_spool_events_kind_check,
    ADD CONSTRAINT filament_spool_events_kind_check CHECK (kind IN (
        'created', 'loaded', 'unloaded', 'moved', 'print_started', 'print_ended', 'weighed',
        'ams_reading', 'dried', 'edited', 'tag_bound', 'tag_unbound', 'archived', 'restored',
        'synced_from_bb', 'consumed_reset', 'non_print_waste'
    ));

-- Backfill: a spool whose newest `moved` event went to a live dry box (a dry-storage place
-- of capacity 1, no printer's place) lives there.
UPDATE filament_spools s SET home_location_id = last_move.target
FROM (
    SELECT DISTINCT ON (e.spool_id) e.spool_id, (e.data ->> 'to')::uuid AS target
    FROM filament_spool_events e
    WHERE e.kind = 'moved'
    ORDER BY e.spool_id, e.at DESC, e.seq DESC
) last_move
JOIN filament_locations l ON l.id = last_move.target
JOIN filament_location_kinds k ON k.id = l.kind_id
WHERE s.id = last_move.spool_id AND k.dry_storage AND l.capacity = 1 AND l.archived_at IS NULL
  AND l.printer_id IS NULL;
