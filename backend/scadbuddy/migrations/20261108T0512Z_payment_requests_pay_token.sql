-- #4960: once the storefront's pay page ships (#5283 phase 4), a Venmo-eligible PayPal
-- request's link is that page, `/pay/<token>`. Only the token's sha256 (hex) is kept,
-- and it is cleared when the request leaves `open`.
ALTER TABLE payment_requests ADD COLUMN pay_token_hash text;
CREATE UNIQUE INDEX payment_requests_pay_token ON payment_requests (pay_token_hash)
  WHERE pay_token_hash IS NOT NULL;
