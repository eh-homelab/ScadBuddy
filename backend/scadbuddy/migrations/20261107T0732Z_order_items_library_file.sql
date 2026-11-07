-- #5544: a library file as a line item. A `library_file` item names a file of
-- Bambuddy's library or of ScadBuddy's own (which one: `library_source`), by id, with
-- its name as it was when the item was added. It has no template reference, no
-- customizer inputs and no output (the `order_items_reference` check's ELSE branch),
-- and it always prints.
ALTER TABLE order_items
  ADD COLUMN IF NOT EXISTS library_file_id bigint,
  ADD COLUMN IF NOT EXISTS library_filename text,
  ADD COLUMN IF NOT EXISTS library_source text
    CHECK (library_source IN ('bambuddy', 'native'));

ALTER TABLE order_items DROP CONSTRAINT order_items_kind_check;
ALTER TABLE order_items
  ADD CONSTRAINT order_items_kind_check
    CHECK (kind IN ('model', 'custom', 'request', 'library_file'));

ALTER TABLE order_items
  ADD CONSTRAINT order_items_library_file CHECK (
    CASE kind
      WHEN 'library_file' THEN library_file_id IS NOT NULL AND library_filename IS NOT NULL
        AND library_source IS NOT NULL AND requires_printing
      ELSE library_file_id IS NULL AND library_filename IS NULL AND library_source IS NULL
    END
  );
