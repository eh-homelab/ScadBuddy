-- #6157: the back office's list of thousands of listings filters and sorts on the server.
-- Its search is a case-insensitive substring match on the title and slug, a scan that is
-- quick at thousands of rows, so it needs no index (and no extension). These plain btree
-- indexes serve the model filter and its sort, and the "updated" sort. The status filter
-- uses `storefront_listings_published` (20261108T0530Z). #6159 adds its own partial index
-- for the public site; neither file names the other's index. IF NOT EXISTS on each, so
-- the order the two files apply in does not matter.
CREATE INDEX IF NOT EXISTS storefront_listings_slug ON storefront_listings (slug, position, id);

CREATE INDEX IF NOT EXISTS storefront_listings_updated ON storefront_listings (updated_at, id);
