-- #7722: the order list sorts and pages by when an order was placed: a storefront order's
-- submit, since its row is made when the visitor's cart opens; any other order's row.
CREATE INDEX IF NOT EXISTS orders_placed ON orders ((COALESCE(submitted_at, created_at)) DESC, id DESC);
