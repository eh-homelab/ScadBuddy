-- The device a queued NTAG write waits on (null for a row queued before this column).
-- A device cancel drops every write queued on it, not only the named spool's.
ALTER TABLE filament_ntag_writes ADD COLUMN device_id text;
