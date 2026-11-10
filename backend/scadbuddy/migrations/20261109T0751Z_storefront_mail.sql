-- #5283 (storefront phase 4, plan Task 2; spec 2026-11-02 §5.7): the storefront's mail.
--
-- `storefront_mail` is the outbox: a row is written in the transaction of the change
-- that causes it and sent after the commit (`orders/storefront/mail.py`). `context` is
-- what the template reads. An order mail names its link by `link_id`; the token is never
-- in the outbox (the sender opens `storefront_order_links.token_sealed`). A sign-in code's
-- `code` and `link` are dropped once the mail is sent, given up, or expired (not sent
-- within 10 minutes of `created_at`). `order_id` is null for a sign-in code. A sender
-- leases a row (`lease_until`) and sends with no transaction open.
-- `attempts` counts the recipient's refusals only; `next_attempt_at` backs them off;
-- `failed_at` is a row given up (its attempts spent, or a failure no retry mends).
CREATE TABLE storefront_mail (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_id text REFERENCES orders (id) ON DELETE CASCADE,
  to_address text NOT NULL,
  template text NOT NULL CHECK (template IN ('sign_in_code', 'order_link', 'revision_ready',
    'price_ready', 'payment_requested', 'shipped')),
  context jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  sent_at timestamptz,
  failed_at timestamptz,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  lease_until timestamptz,
  last_error text,
  CHECK (sent_at IS NULL OR failed_at IS NULL)
);
-- What the sender claims: the rows neither sent nor given up, oldest first.
CREATE INDEX storefront_mail_unsent ON storefront_mail (created_at, id)
  WHERE sent_at IS NULL AND failed_at IS NULL;
CREATE INDEX storefront_mail_order ON storefront_mail (order_id, created_at);

-- The SMTP password, sealed under the CRUD service's KEK (core/secrets.py), as payment
-- credentials are. One row at most. It is bound to the connection it was saved for: the
-- host, port, user and TLS mode beside it are also in the seal's AAD
-- (`storefront_mail_secret:<id>:<host>:<port>:<user>:<tls>`), and the sender logs in only
-- while the live settings name the same four.
CREATE TABLE storefront_mail_secret (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  singleton boolean NOT NULL DEFAULT true UNIQUE CHECK (singleton),
  host text NOT NULL,
  port integer NOT NULL,
  username text NOT NULL,
  tls text NOT NULL CHECK (tls IN ('starttls', 'implicit', 'none')),
  secret_sealed bytea NOT NULL,
  dek_sealed bytea NOT NULL,
  kek_id text NOT NULL,
  saved_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- The last failure of the SMTP server or its setup (a connect, TLS, login or sender
-- failure, a password bound to another connection), which no row is charged for. The
-- shop reads it; a send that succeeds clears it. One row at most.
CREATE TABLE storefront_mail_server (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  last_error text,
  last_error_at timestamptz
);

-- An order's links for the customer (plan Task 3 issues them; `storefront_mail_covers`
-- reads them). `token_sha256` (hex) finds a link; `token_sealed` is the token sealed
-- under the KEK (AAD `storefront_order_links:<id>`), opened only to write it into a mail.
-- A revoked link keeps no sealed token. `sent_to` is the address its mail went to, where
-- the order's later mail goes too.
CREATE TABLE storefront_order_links (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  token_sha256 text NOT NULL UNIQUE,
  token_sealed bytea,
  dek_sealed bytea,
  kek_id text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  revoked_at timestamptz,
  sent_to text NOT NULL,
  opened_at timestamptz,
  CHECK ((token_sealed IS NULL) = (dek_sealed IS NULL) AND (token_sealed IS NULL) = (kek_id IS NULL)),
  CHECK (revoked_at IS NULL OR token_sealed IS NULL)
);
CREATE INDEX storefront_order_links_live ON storefront_order_links (order_id, created_at)
  WHERE revoked_at IS NULL;
