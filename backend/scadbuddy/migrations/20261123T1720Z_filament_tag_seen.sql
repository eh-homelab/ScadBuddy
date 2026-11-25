-- #7273 R1: when a tag was last read, by which source, and the NDEF records it held.
ALTER TABLE filament_tags
    ADD COLUMN last_seen_at timestamptz,
    ADD COLUMN last_seen_source text CHECK (last_seen_source IN ('phone', 'agent', 'ams')),
    ADD COLUMN last_seen_records jsonb;
