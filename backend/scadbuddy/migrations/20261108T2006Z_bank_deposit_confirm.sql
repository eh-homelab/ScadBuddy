-- A suggested deposit confirmed or dismissed on its order (#4961). `payment_id` is the
-- payment the shop recorded from the deposit (method `bank_transfer`, reference the Plaid
-- transaction id); a dismissed suggestion keeps its row, so a later sync does not suggest
-- it again.
ALTER TABLE bank_deposits
  ADD COLUMN payment_id text REFERENCES order_payments (id) ON DELETE SET NULL;
ALTER TABLE bank_deposit_matches ADD COLUMN dismissed_at timestamptz;
