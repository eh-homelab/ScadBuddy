-- #5533: `OrderStore.fulfilled_items` (the quote warning's history, #5328) reads the
-- items of one slug in one status on every pause in typing. No index served it.
-- The fulfilments side of its LATERAL needs no index of its own: the
-- `UNIQUE (order_item_id, attempt)` of 20261020T0446Z_orders.sql leads with its join key.
CREATE INDEX IF NOT EXISTS order_items_slug_status ON order_items (slug, status);
