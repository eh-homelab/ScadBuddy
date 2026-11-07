-- #5329: the profit report (`OrderStore.accepted_orders`) reads a period's orders by
-- `accepted_at`, newest first, in batches of 100 from a keyset position. No index served
-- it, so each batch was a scan and a sort of every order.
CREATE INDEX IF NOT EXISTS orders_accepted_at ON orders (accepted_at DESC, id DESC);
