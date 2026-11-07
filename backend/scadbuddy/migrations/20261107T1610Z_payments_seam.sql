-- The payments seam (#4957): the shop's own credentials per provider (sealed), the
-- payment requests a customer pays through a hosted link, and the ledger of provider
-- events that makes a replayed webhook record one payment.

-- One row per connected provider. The secret is sealed (core/secrets.py, AAD
-- `payment_credentials:<id>:<provider>`); `settings` holds non-secret fields only.
CREATE TABLE payment_credentials (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  provider text NOT NULL UNIQUE CHECK (provider ~ '^[a-z][a-z0-9_]{0,39}$'),
  account_label text NOT NULL,
  settings jsonb NOT NULL DEFAULT '{}',
  secret_sealed bytea NOT NULL,
  dek_sealed bytea NOT NULL,
  kek_id text NOT NULL,
  is_default boolean NOT NULL DEFAULT false,
  connected_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  last_webhook_at timestamptz
);
-- At most one default provider.
CREATE UNIQUE INDEX payment_credentials_default ON payment_credentials ((true)) WHERE is_default;

-- A request for money on an order. With no provider there is no link: the shop collects
-- by hand. `payment_id` is the payment that settled it (foreign key added below).
CREATE TABLE payment_requests (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('deposit', 'balance')),
  provider text,
  provider_ref text,
  hosted_url text,
  amount_minor bigint NOT NULL CHECK (amount_minor > 0),
  currency text NOT NULL,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'paid', 'expired', 'cancelled')),
  link_error text,
  payment_id text,
  created_by jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  paid_at timestamptz,
  CHECK ((status = 'paid') = (paid_at IS NOT NULL)),
  CHECK (provider IS NOT NULL OR (provider_ref IS NULL AND hosted_url IS NULL))
);
CREATE INDEX payment_requests_order ON payment_requests (order_id, created_at);
CREATE UNIQUE INDEX payment_requests_ref ON payment_requests (provider, provider_ref) WHERE provider_ref IS NOT NULL;

ALTER TABLE order_payments ADD COLUMN provider text;
ALTER TABLE order_payments ADD COLUMN payment_request_id text REFERENCES payment_requests (id);
ALTER TABLE payment_requests ADD FOREIGN KEY (payment_id) REFERENCES order_payments (id);

-- One row per provider event, keyed by the provider's own event id. No body column: an
-- event's payload is never stored.
CREATE TABLE payment_events (
  id bigserial PRIMARY KEY,
  provider text NOT NULL,
  event_id text NOT NULL,
  kind text NOT NULL,
  outcome text CHECK (outcome IN ('recorded', 'replayed', 'unmatched', 'ignored', 'currency_mismatch')),
  order_id text REFERENCES orders (id) ON DELETE SET NULL,
  payment_request_id text REFERENCES payment_requests (id) ON DELETE SET NULL,
  payment_id text REFERENCES order_payments (id) ON DELETE SET NULL,
  received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (provider, event_id)
);
