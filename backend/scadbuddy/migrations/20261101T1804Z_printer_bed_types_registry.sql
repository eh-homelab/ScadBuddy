-- #4971 (tier 3b, part 1): the plate remembered per printer is keyed by the registry
-- printer (`printers.id`), so a LAN-only printer, which has no Bambuddy id, remembers one
-- too. Expand only: `printer_id` (the Bambuddy printer) stays, nullable, because the
-- previous build still reads and upserts it (`ON CONFLICT (printer_id)`) during a rolling
-- deploy; this build writes both where the registry knows both, and reads a row the
-- previous build wrote through the registry's Bambuddy link
-- (`library/settings_store.py` `_PLATES`). Dropping `printer_id` is a follow-up that
-- merges once this release runs everywhere.
ALTER TABLE printer_bed_types
    ADD COLUMN printer uuid REFERENCES printers (id) ON DELETE CASCADE;

UPDATE printer_bed_types b SET printer = p.id
    FROM printers p
    WHERE p.bambuddy_printer_id = b.printer_id AND b.printer IS NULL;

ALTER TABLE printer_bed_types DROP CONSTRAINT printer_bed_types_pkey;
ALTER TABLE printer_bed_types
    ALTER COLUMN printer_id DROP NOT NULL,
    -- The previous build's `ON CONFLICT (printer_id)` infers this index.
    ADD CONSTRAINT printer_bed_types_printer_id_key UNIQUE (printer_id),
    ADD CONSTRAINT printer_bed_types_printer_key UNIQUE (printer),
    ADD CONSTRAINT printer_bed_types_keyed CHECK (printer IS NOT NULL OR printer_id IS NOT NULL);
