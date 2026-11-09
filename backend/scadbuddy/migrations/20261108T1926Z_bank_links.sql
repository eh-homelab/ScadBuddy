-- The shop's bank account linked through Plaid (#4961), beside the payments seam: Plaid
-- opens no payment request, so it is not a `payment_credentials` row. One row at most.
-- `secret_sealed` holds the Plaid client id, secret and, once linked, the access token,
-- sealed (core/secrets.py, AAD `bank_links:<id>`). In the clear: Plaid's ids and the
-- account's name, type and mask (its last 2-4 characters) only; never an account or
-- routing number.
CREATE TABLE bank_links (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  singleton boolean NOT NULL DEFAULT true UNIQUE CHECK (singleton),
  environment text NOT NULL CHECK (environment IN ('sandbox', 'production')),
  secret_sealed bytea NOT NULL,
  dek_sealed bytea NOT NULL,
  kek_id text NOT NULL,
  saved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  item_id text,
  account_id text,
  account_name text,
  account_type text CHECK (account_type IN ('checking', 'savings')),
  account_mask text,
  linked_at timestamptz,
  CHECK (
    (item_id IS NULL AND account_id IS NULL AND account_name IS NULL
      AND account_type IS NULL AND account_mask IS NULL AND linked_at IS NULL)
    OR (item_id IS NOT NULL AND account_id IS NOT NULL AND account_name IS NOT NULL
      AND account_type IS NOT NULL AND linked_at IS NOT NULL)
  )
);
