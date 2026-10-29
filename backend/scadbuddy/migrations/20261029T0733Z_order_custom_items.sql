-- Custom line items (#4954): an item with a title, a quantity and a typed price, and no
-- template behind it. A `model` item names what to make (the reference columns, as
-- before); a `custom` item names nothing, so those columns are null for it.
-- `requires_printing` is the custom item's own flag (a model item always prints), and
-- `sku` is any item's optional stock code.
ALTER TABLE order_items
  ADD COLUMN kind text NOT NULL DEFAULT 'model' CHECK (kind IN ('model', 'custom')),
  ADD COLUMN requires_printing boolean NOT NULL DEFAULT true,
  ADD COLUMN sku text,
  ALTER COLUMN slug DROP NOT NULL,
  ALTER COLUMN commit DROP NOT NULL,
  ALTER COLUMN inputs DROP NOT NULL,
  ALTER COLUMN template_name DROP NOT NULL,
  ALTER COLUMN render_key DROP NOT NULL;

ALTER TABLE order_items
  ADD CONSTRAINT order_items_reference CHECK (
    CASE kind
      WHEN 'model' THEN slug IS NOT NULL AND commit IS NOT NULL AND inputs IS NOT NULL
        AND template_name IS NOT NULL AND render_key IS NOT NULL AND requires_printing
      ELSE slug IS NULL AND commit IS NULL AND inputs IS NULL AND template_name IS NULL
        AND render_key IS NULL AND output_id IS NULL
    END
  );
