-- #5153, the contract step of #4971 part 1 (`20261101T1804Z_printer_bed_types_registry.sql`):
-- the plate remembered per printer is keyed by the registry printer (`printers.id`) only.
-- No build that reads or writes `printer_id` (the Bambuddy printer) runs any more, so it
-- goes; the Bambuddy id is read through the registry's link
-- (`library/settings_store.py` `_PLATES`).

-- A row the build before part 1 wrote after part 1's backfill names only the Bambuddy
-- printer. Where the registry printer that links it also has a row of its own (written
-- before the link), the newer of the two is the plate last printed on.
DELETE FROM printer_bed_types AS stale
    USING printers AS p, printer_bed_types AS own
    WHERE stale.printer IS NULL AND p.bambuddy_printer_id = stale.printer_id
        AND own.printer = p.id AND own.updated_at >= stale.updated_at;
DELETE FROM printer_bed_types AS own
    USING printers AS p, printer_bed_types AS stale
    WHERE stale.printer IS NULL AND p.bambuddy_printer_id = stale.printer_id
        AND own.printer = p.id AND own.updated_at < stale.updated_at;

UPDATE printer_bed_types AS b SET printer = p.id
    FROM printers AS p
    WHERE p.bambuddy_printer_id = b.printer_id AND b.printer IS NULL;

-- A Bambuddy printer the registry never held: the sync (#4439) makes a row for each one
-- with a serial, and a print run makes one for any it prints on, so nothing reads these.
DELETE FROM printer_bed_types WHERE printer IS NULL;

ALTER TABLE printer_bed_types
    DROP CONSTRAINT printer_bed_types_keyed,
    DROP CONSTRAINT printer_bed_types_printer_id_key,
    DROP CONSTRAINT printer_bed_types_printer_key,
    DROP COLUMN printer_id,
    ALTER COLUMN printer SET NOT NULL,
    ADD CONSTRAINT printer_bed_types_pkey PRIMARY KEY (printer);
