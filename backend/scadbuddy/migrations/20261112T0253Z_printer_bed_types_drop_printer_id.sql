-- #6059, the contract step after #5153
-- (`20261111T1842Z_printer_bed_types_backfill_registry.sql`): every pod runs a build that
-- reads and writes the plate remembered per printer by the registry printer
-- (`printers.id`) only. This file drops `printer_id` (the Bambuddy printer), its unique
-- constraint and the `keyed` check, and makes `printer` the primary key.

-- Rows the build before #6054 wrote during its rollout name only the Bambuddy printer
-- (`printer` NULL), and no build after #6054 reads them. The registry printer that links
-- one can also have a row that #6054's build wrote (unique on `printer`). Keep the newer
-- row of each such pair, as `20261111T1842Z_printer_bed_types_backfill_registry.sql`
-- does, before the backfill, or the backfill collides with it.
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
-- A Bambuddy printer no registry printer links: no build after #6054 reads it.
DELETE FROM printer_bed_types WHERE printer IS NULL;

ALTER TABLE printer_bed_types
    DROP CONSTRAINT IF EXISTS printer_bed_types_keyed,
    DROP CONSTRAINT IF EXISTS printer_bed_types_printer_id_key,
    DROP CONSTRAINT IF EXISTS printer_bed_types_printer_key,
    DROP COLUMN printer_id,
    ALTER COLUMN printer SET NOT NULL,
    ADD CONSTRAINT printer_bed_types_pkey PRIMARY KEY (printer);
