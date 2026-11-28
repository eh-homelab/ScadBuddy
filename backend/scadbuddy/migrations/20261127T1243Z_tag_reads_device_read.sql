-- #8244 (plan docs/superpowers/plans/2026-11-27-devices.md, Task 6): a tag read made from a
-- paired device's read (POST /api/v1/filament/tags/reads with `read_id`) names that
-- `device_reads` row. A device read makes at most one history row: a second post of the same
-- read answers the first row.
--
-- No foreign key: a history row is never changed or deleted (`filament_tag_reads_keep`), and
-- a device read goes with its device (ON DELETE CASCADE). So the id stays as a plain record.
ALTER TABLE filament_tag_reads ADD COLUMN device_read_id uuid;
CREATE UNIQUE INDEX filament_tag_reads_device_read ON filament_tag_reads (device_read_id)
    WHERE device_read_id IS NOT NULL;
