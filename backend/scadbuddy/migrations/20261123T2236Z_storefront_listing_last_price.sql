-- #6710 §7.5: a file listing's price read answers the listing's last firm price of its
-- file, of any colours, while the visitor's colours are still being priced
-- (`SlicePrices.listing_last`).
CREATE INDEX storefront_slice_prices_listing_last
  ON storefront_slice_prices (listing_id, model_sha256, finished_at DESC)
  WHERE status = 'done' AND listing_id IS NOT NULL;
