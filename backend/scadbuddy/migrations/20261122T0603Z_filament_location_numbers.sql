-- `number` is the figure a person reads on a location label (`L-12`). It is never reused:
-- an archived place keeps its number. Adding the column numbers the existing rows.
ALTER TABLE filament_locations ADD COLUMN number integer GENERATED ALWAYS AS IDENTITY UNIQUE;

-- A printer is a place a spool is kept, so it has a kind of its own. `printer` marks the kind
-- a printer's own place takes, as `ams` marks an AMS unit's.
ALTER TABLE filament_location_kinds ADD COLUMN printer boolean NOT NULL DEFAULT false;

INSERT INTO filament_location_kinds (name, sort_order, dry_storage, ams, printer)
SELECT 'Printer', 70, false, false, true
WHERE NOT EXISTS (
    SELECT 1 FROM filament_location_kinds WHERE upper(name) = 'PRINTER' AND archived_at IS NULL
);
UPDATE filament_location_kinds SET printer = true
WHERE upper(name) = 'PRINTER' AND archived_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM filament_location_kinds WHERE printer AND archived_at IS NULL);
