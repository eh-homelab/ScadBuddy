-- A suggested deposit confirmed or dismissed on its order (#4961). `payment_id` is the
-- payment the shop recorded from the deposit (method `bank_transfer`, reference the Plaid
-- transaction id); a dismissal is a `bank_deposit_dismissals` row, so no later sync
-- suggests that pair again, after an unlink and a new link too.
ALTER TABLE bank_deposits
  ADD COLUMN payment_id text REFERENCES order_payments (id) ON DELETE SET NULL,
  -- Plaid removed a recorded deposit: the row stays, so its order page can say so.
  ADD COLUMN removed_at timestamptz,
  -- Plaid changed a recorded deposit's amount, at least once: kept, so the order page
  -- checks the payments against the bank's amount from then on.
  ADD COLUMN changed_at timestamptz;
-- "Not this order" lives apart from the deposits, which an unlink deletes: a new link's
-- sync reads the same transactions again and must not suggest a dismissed pair.
CREATE TABLE bank_deposit_dismissals (
  transaction_id text NOT NULL,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  dismissed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (transaction_id, order_id)
);
