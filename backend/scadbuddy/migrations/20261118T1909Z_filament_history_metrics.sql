-- A type's own reorder timing (#6269); null falls back to the filament_lead_time_days and
-- filament_safety_margin_days settings.
ALTER TABLE filament_spool_types
    ADD COLUMN lead_time_days integer CHECK (lead_time_days >= 0),
    ADD COLUMN safety_margin_days integer CHECK (safety_margin_days >= 0);

-- The inventory reads count plates only, so they skip the AMS readings.
CREATE INDEX filament_spool_events_plates ON filament_spool_events (spool_id, at)
    WHERE kind IN ('print_started', 'print_ended');
