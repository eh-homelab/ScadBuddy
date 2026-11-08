-- #5684: the last webhook failure per provider, shown in Settings → Payments. Only a
-- short fixed category and its time: never the provider's response body or an
-- exception's text, which can echo secrets or customer data.
ALTER TABLE payment_credentials
  ADD COLUMN last_webhook_error text
    CHECK (last_webhook_error IN ('signature', 'provider_unavailable', 'out_of_order', 'handler_error')),
  ADD COLUMN last_webhook_error_at timestamptz,
  ADD CONSTRAINT payment_credentials_webhook_error_at
    CHECK ((last_webhook_error IS NULL) = (last_webhook_error_at IS NULL));
