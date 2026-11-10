-- #5283 (storefront phase 4, spec 2026-11-02 §5.2): a visitor's account, made by a
-- passwordless sign-in. Only sha256 digests of codes and session tokens are stored, never
-- the code or the token (`orders/storefront/accounts.py`).

-- One account per verified email (lowercased), linked to one customer.
CREATE TABLE storefront_accounts (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  email text NOT NULL UNIQUE CHECK (email = lower(email)),
  customer_id text NOT NULL REFERENCES customers (id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_sign_in_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX storefront_accounts_customer ON storefront_accounts (customer_id);

-- A six-digit code sent to an address. `client` is the address that asked, for the
-- per-client limit. Only the newest unused code of an address is good.
CREATE TABLE storefront_sign_in_codes (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  email text NOT NULL,
  client text NOT NULL,
  code_sha256 text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  attempts integer NOT NULL DEFAULT 0,
  used_at timestamptz
);
CREATE INDEX storefront_sign_in_codes_email ON storefront_sign_in_codes (email, created_at);
CREATE INDEX storefront_sign_in_codes_client ON storefront_sign_in_codes (client, created_at);
CREATE INDEX storefront_sign_in_codes_created ON storefront_sign_in_codes (created_at);

-- A wrong code, by the client that sent it: the per-client guess limit.
CREATE TABLE storefront_sign_in_failures (
  client text NOT NULL,
  failed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX storefront_sign_in_failures_client ON storefront_sign_in_failures (client, failed_at);
CREATE INDEX storefront_sign_in_failures_at ON storefront_sign_in_failures (failed_at);

-- A signed-in session. Signing out deletes its row.
CREATE TABLE storefront_sessions (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  account_id text NOT NULL REFERENCES storefront_accounts (id) ON DELETE CASCADE,
  token_sha256 text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL
);
CREATE INDEX storefront_sessions_account ON storefront_sessions (account_id);

-- The visitor that signed in belongs to the account: its orders are the account's.
ALTER TABLE storefront_visitors
  ADD COLUMN account_id text REFERENCES storefront_accounts (id) ON DELETE SET NULL;
CREATE INDEX storefront_visitors_account ON storefront_visitors (account_id);

-- An order whose link was opened while signed in (spec §5.2 "What an account sees").
CREATE TABLE storefront_account_orders (
  account_id text NOT NULL REFERENCES storefront_accounts (id) ON DELETE CASCADE,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  attached_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (account_id, order_id)
);
CREATE INDEX storefront_account_orders_order ON storefront_account_orders (order_id);
