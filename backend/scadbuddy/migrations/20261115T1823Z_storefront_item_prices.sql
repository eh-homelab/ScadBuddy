-- #6153 (spec 2026-11-13 §5.4): the firm price of a storefront cart line. The server
-- reads it when the visitor adds the line, and again at checkout, from the variant's
-- `price_key` (`orders/storefront/line_prices.py`). It never takes a price from the
-- browser.
--
-- One row per listing line of a cart. `listing_id` is the listing it was added from, so
-- the checkout sees a design that moved. `model_sha256` and `slot_filaments` are what
-- the key was made from when the line was added (the 3MF's content and the price-book
-- filament of each slot): the render job is pruned after a day, and the cart lives
-- longer. `price_key` and `unit_price_minor` are the price the visitor saw, or NULL when
-- the line had no firm price (the shop prices it by hand).
CREATE TABLE storefront_item_prices (
  order_item_id text PRIMARY KEY REFERENCES order_items (id) ON DELETE CASCADE,
  listing_id text NOT NULL,
  model_sha256 text NOT NULL,
  slot_filaments text[] NOT NULL,
  price_key text,
  unit_price_minor bigint,
  currency text,
  priced_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK ((price_key IS NULL) = (unit_price_minor IS NULL)),
  CHECK ((price_key IS NULL) = (currency IS NULL))
);

-- Each plate a storefront variant's slice gave (`orders/pricing.py` `Plate`: its index,
-- seconds and grams per slot), so a checkout's quote carries the slice's facts for job
-- cost. NULL on a row written before: the quote then sums `print_seconds` and `grams`.
ALTER TABLE storefront_slice_prices ADD COLUMN plates jsonb;

-- A quote from the storefront's slice price: the line's firm price at checkout. It names
-- no sliced file (the storefront's slice keeps none), so a print slices again.
ALTER TABLE order_item_quotes DROP CONSTRAINT order_item_quotes_source_check;
ALTER TABLE order_item_quotes ADD CONSTRAINT order_item_quotes_source_check
  CHECK (source IN ('slice', 'manual', 'archive', 'storefront'));
