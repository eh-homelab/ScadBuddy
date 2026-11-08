-- #5666: the one currency a connected account takes payments in, when it has one (a
-- Square location's), as its provider's `verify` gave it. Settings → Payments warns when
-- the shop's `orders_currency` differs. NULL: an account that takes any currency.
ALTER TABLE payment_credentials
  ADD COLUMN currency text,
  ADD CONSTRAINT payment_credentials_currency CHECK (currency ~ '^[A-Z]{3}$');
