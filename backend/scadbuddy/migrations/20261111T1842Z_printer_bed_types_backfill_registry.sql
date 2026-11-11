-- #5153, after #4971 part 1 (`20261101T1804Z_printer_bed_types_registry.sql`): this build
-- reads and writes the plate remembered per printer by the registry printer
-- (`printers.id`) only, and reads its Bambuddy id through the registry's link
-- (`library/settings_store.py` `_PLATES`). This file only moves the data. `printer_id`
-- (the Bambuddy printer) stays, nullable, with its unique constraint and the `keyed`
-- check. The reason: during the rolling deploy, the previous build still reads it and
-- writes it (`INSERT … ON CONFLICT (printer_id)`, `DELETE … OR printer_id = %s`,
-- `UPDATE … SET printer_id = NULL`). This build never reads or writes it. #6059 drops
-- `printer_id` and its constraints and makes `printer` the primary key. It merges once
-- this release runs on every pod.

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
-- with a serial, and a print run makes one for any it prints on, so this build reads
-- none of these.
DELETE FROM printer_bed_types WHERE printer IS NULL;
