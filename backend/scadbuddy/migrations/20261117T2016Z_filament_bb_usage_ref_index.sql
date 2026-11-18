-- `bambuddy_usage.unbooked` asks which BB usage rows a `bb_usage` event names
-- (`ref_kind = %s AND ref_id = ANY(..)`); the (spool, kind, ref) indexes lead with the
-- spool, so none serves it (final review of #6263, M3).
CREATE INDEX IF NOT EXISTS filament_spool_events_ref
    ON filament_spool_events (ref_kind, ref_id);
