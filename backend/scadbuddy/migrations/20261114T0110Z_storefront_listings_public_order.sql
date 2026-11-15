-- #6154: the public list pages the published listings by (position, id), keyset, so a
-- later page of thousands costs what the first does. Partial: only published rows are
-- read there. Title search is an ILIKE over these rows; no extension is needed.
CREATE INDEX IF NOT EXISTS storefront_listings_published_order
  ON storefront_listings (position, id) WHERE published;
