-- #6921: the storefront shows the last price and preview at once. A price row is keyed by
-- the render's 3MF content (`price_key`), which is known only once the render is done. The
-- variant key is known before the render: the sha256 of the listing id, the listing's
-- published version (`updated_at`) and the visitor's choices as the CRUD side normalized
-- them (`orders/storefront/renders.py` `variant_key`). The visitor's render row keeps it.
-- A `done` price row records the variant key of the render it priced, the price-book
-- filament of each slot of that render (`slot_filaments`, from 0) and each slot's colour
-- (`slot_colours`), so the instant read can build the price key again as the shop's
-- settings stand now, and the output its slice saved (`output_id`, "Storefront: ..."),
-- whose preview outlives the render prune. Rows from before stay NULL.
ALTER TABLE storefront_renders ADD COLUMN variant_key text;
ALTER TABLE storefront_slice_prices ADD COLUMN variant_key text;
ALTER TABLE storefront_slice_prices ADD COLUMN slot_filaments jsonb;
ALTER TABLE storefront_slice_prices ADD COLUMN slot_colours jsonb;
ALTER TABLE storefront_slice_prices ADD COLUMN output_id text;

CREATE INDEX storefront_slice_prices_variant
  ON storefront_slice_prices (variant_key, finished_at DESC)
  WHERE status = 'done' AND variant_key IS NOT NULL;
