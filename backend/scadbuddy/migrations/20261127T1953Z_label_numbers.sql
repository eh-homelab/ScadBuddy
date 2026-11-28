-- A number for every spool type and every item, from one sequence (#7169, design D3).
-- A spool type becomes an item with the same id later. With one sequence, a type's number
-- is never an item's number, so the type's item row can keep the type's number and a
-- printed label needs no renumber. Each table's numbers have gaps. The sequence is not
-- OWNED BY a column, so dropping one table keeps the other's default.
CREATE SEQUENCE item_numbers_seq AS integer;

ALTER TABLE filament_spool_types ADD COLUMN number integer;
ALTER TABLE items ADD COLUMN number integer;

-- The types first, then the items, each by created_at and id.
UPDATE filament_spool_types t SET number = n.rn
FROM (SELECT id, row_number() OVER (ORDER BY created_at, id) AS rn
      FROM filament_spool_types) n
WHERE t.id = n.id;

UPDATE items i SET number = n.rn + (SELECT coalesce(max(number), 0) FROM filament_spool_types)
FROM (SELECT id, row_number() OVER (ORDER BY created_at, id) AS rn FROM items) n
WHERE i.id = n.id;

SELECT setval('item_numbers_seq',
    greatest((SELECT coalesce(max(number), 0) FROM filament_spool_types),
             (SELECT coalesce(max(number), 0) FROM items)) + 1,
    false);

ALTER TABLE filament_spool_types
    ALTER COLUMN number SET DEFAULT nextval('item_numbers_seq'),
    ALTER COLUMN number SET NOT NULL,
    ADD CONSTRAINT filament_spool_types_number_key UNIQUE (number);
ALTER TABLE items
    ALTER COLUMN number SET DEFAULT nextval('item_numbers_seq'),
    ALTER COLUMN number SET NOT NULL,
    ADD CONSTRAINT items_number_key UNIQUE (number);
