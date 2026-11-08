-- The linked account's deposits and the orders each may pay (#4961). A suggestion is
-- never a payment: the shop confirms it on the order. The sync's cursor and its last
-- outcome live on the link. A deposit's `description` is the bank's text with every run
-- of five or more digits masked, so no account number is kept.
ALTER TABLE bank_links
  ADD COLUMN sync_cursor text,
  ADD COLUMN synced_at timestamptz,
  ADD COLUMN sync_error text;

CREATE TABLE bank_deposits (
  transaction_id text PRIMARY KEY,
  item_id text NOT NULL,
  amount_minor bigint NOT NULL CHECK (amount_minor > 0),
  currency text NOT NULL,
  posted_on date NOT NULL,
  description text NOT NULL,
  seen_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX bank_deposits_item ON bank_deposits (item_id);

CREATE TABLE bank_deposit_matches (
  transaction_id text NOT NULL REFERENCES bank_deposits (transaction_id) ON DELETE CASCADE,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  reason text NOT NULL CHECK (reason IN ('amount', 'order_number')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (transaction_id, order_id)
);
CREATE INDEX bank_deposit_matches_order ON bank_deposit_matches (order_id);
