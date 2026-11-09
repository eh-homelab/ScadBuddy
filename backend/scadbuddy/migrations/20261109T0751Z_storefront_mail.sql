-- #5283 (storefront phase 4, plan Task 2; spec 2026-11-02 §5.7): the storefront's mail.
--
-- `storefront_mail` is the outbox: a row is written in the transaction of the change
-- that causes it and sent after the commit (`orders/storefront/mail.py`). `context` is
-- what the template reads; a token or a sign-in code in it is dropped once the mail is
-- sent or given up. `order_id` is null for a sign-in code.
CREATE TABLE storefront_mail (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_id text REFERENCES orders (id) ON DELETE CASCADE,
  to_address text NOT NULL,
  template text NOT NULL CHECK (template IN ('sign_in_code', 'order_link', 'revision_ready',
    'price_ready', 'payment_requested', 'shipped')),
  context jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  sent_at timestamptz,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  last_error text
);
-- What the sender claims: the rows not yet sent, oldest first.
CREATE INDEX storefront_mail_unsent ON storefront_mail (created_at, id) WHERE sent_at IS NULL;
CREATE INDEX storefront_mail_order ON storefront_mail (order_id, created_at);

-- The SMTP password, sealed under the CRUD service's KEK (core/secrets.py, AAD
-- `storefront_mail_secret:<id>`), as payment credentials are. One row at most.
CREATE TABLE storefront_mail_secret (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  singleton boolean NOT NULL DEFAULT true UNIQUE CHECK (singleton),
  secret_sealed bytea NOT NULL,
  dek_sealed bytea NOT NULL,
  kek_id text NOT NULL,
  saved_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- An order's links for the customer (plan Task 3 issues them; this task's
-- `storefront_mail_covers` reads them). Only the sha256 (hex) of a token is kept. A live
-- link is one not revoked; `sent_to` is the address its mail went to, where the order's
-- later mail goes too.
CREATE TABLE storefront_order_links (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  token_sha256 text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  revoked_at timestamptz,
  sent_to text NOT NULL,
  opened_at timestamptz
);
CREATE INDEX storefront_order_links_live ON storefront_order_links (order_id, created_at)
  WHERE revoked_at IS NULL;
