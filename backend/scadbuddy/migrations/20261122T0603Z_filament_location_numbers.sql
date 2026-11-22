-- `number` is the figure a person reads on a location label (`L-12`). It is never reused:
-- an archived place keeps its number. Adding the column numbers the existing rows.
ALTER TABLE filament_locations ADD COLUMN number integer GENERATED ALWAYS AS IDENTITY UNIQUE;

-- A printer is a place a spool is kept, so it has a kind of its own.
INSERT INTO filament_location_kinds (name, sort_order, dry_storage, ams)
SELECT 'Printer', 70, false, false
WHERE NOT EXISTS (
    SELECT 1 FROM filament_location_kinds WHERE upper(name) = 'PRINTER' AND archived_at IS NULL
);
