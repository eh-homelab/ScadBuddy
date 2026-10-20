-- Orders phase 1 (#4249, spec 2026-10-18 §3.1-3.4, §6.1, §6.3; plan 2026-10-19
-- Rulings 1-5): customers with their contact points and addresses, orders with their
-- line items, quotes, fulfilment attempts, shipments, charges, payments and audit line.
-- Money is integer minor units in the order's `currency` (§3.6). Ids are uuids as text,
-- so a topic (`order:<id>`) and a route carry them as every other id. A table listed in
-- the order its rows were written (quotes, payments, shipments) stamps `created_at` with
-- `clock_timestamp()`, which advances within a transaction where `now()` does not
-- (https://www.postgresql.org/docs/17/functions-datetime.html#FUNCTIONS-DATETIME-CURRENT).

CREATE TABLE customers (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  display_name text NOT NULL,
  company text,
  notes text NOT NULL DEFAULT '',
  tags text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz
);

CREATE TABLE customer_contacts (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  customer_id text NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('phone', 'email', 'instagram', 'facebook', 'tiktok', 'etsy', 'website', 'other')),
  value text NOT NULL,
  label text,
  "primary" boolean NOT NULL DEFAULT false,
  position integer NOT NULL
);
CREATE INDEX customer_contacts_customer ON customer_contacts (customer_id, position);
-- §3.1: at most one primary contact per kind.
CREATE UNIQUE INDEX customer_contacts_primary ON customer_contacts (customer_id, kind) WHERE "primary";

CREATE TABLE customer_addresses (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  customer_id text NOT NULL REFERENCES customers (id) ON DELETE CASCADE,
  label text,
  recipient text NOT NULL,
  lines text[] NOT NULL DEFAULT '{}',
  locality text,
  region text,
  postal_code text,
  country text CHECK (country IS NULL OR country ~ '^[A-Z]{2}$'),
  phone text,
  default_shipping boolean NOT NULL DEFAULT false,
  default_billing boolean NOT NULL DEFAULT false,
  position integer NOT NULL
);
CREATE INDEX customer_addresses_customer ON customer_addresses (customer_id, position);

-- Ruling 5: the per-year counter an order's number is taken from under a row lock,
-- in the insert's transaction, so a rolled-back insert skips nothing. Never a sequence.
CREATE TABLE order_numbers (
  year integer PRIMARY KEY,
  last integer NOT NULL
);

CREATE TABLE orders (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  number text NOT NULL UNIQUE,
  customer_id text NOT NULL REFERENCES customers (id),
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'quoted', 'accepted', 'in_production', 'ready', 'shipped', 'closed', 'cancelled')),
  currency char(3) NOT NULL,
  on_hold boolean NOT NULL DEFAULT false,
  -- Ruling 11: the customer's rows while the order is open; snapshots taken at accept.
  shipping_address_id text REFERENCES customer_addresses (id) ON DELETE SET NULL,
  billing_address_id text REFERENCES customer_addresses (id) ON DELETE SET NULL,
  shipping_address jsonb,
  billing_address jsonb,
  notes text NOT NULL DEFAULT '',
  due_at timestamptz,
  accepted_at timestamptz,
  closed_at timestamptz,
  cancelled_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  created_by jsonb NOT NULL
);
CREATE INDEX orders_customer ON orders (customer_id);
CREATE INDEX orders_status_created ON orders (status, created_at DESC);

CREATE TABLE order_charges (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('shipping', 'discount', 'fee', 'tax')),
  label text NOT NULL,
  amount_minor bigint NOT NULL,
  position integer NOT NULL
);
CREATE INDEX order_charges_order ON order_charges (order_id, position);

CREATE TABLE order_payments (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  amount_minor bigint NOT NULL,
  method text,
  reference text,
  received_at timestamptz,
  notes text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX order_payments_order ON order_payments (order_id, created_at);

-- §3.2: the audit line, written in the writer's transaction (Ruling 12); `detail`
-- holds ids and the before/after status only.
CREATE TABLE order_events (
  id bigserial PRIMARY KEY,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  at timestamptz NOT NULL DEFAULT now(),
  by jsonb NOT NULL,
  kind text NOT NULL,
  detail jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX order_events_order ON order_events (order_id, id);

-- §3.3: the reference is `slug`, `commit` (full, Ruling 1), `inputs`, `libraries`;
-- the rest is display and the output it was taken from. `status` carries every §3.5
-- state so phase 3's print path (`queued`, `printing`, `printed`) adds no migration.
CREATE TABLE order_items (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  position integer NOT NULL,
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'quoted', 'approved', 'queued', 'printing', 'printed', 'fulfilled', 'failed', 'cancelled')),
  quantity integer NOT NULL CHECK (quantity >= 1),
  title text NOT NULL,
  notes text NOT NULL DEFAULT '',
  slug text NOT NULL,
  commit text NOT NULL CHECK (commit ~ '^[0-9a-f]{40}$'),
  inputs jsonb NOT NULL,
  libraries jsonb NOT NULL DEFAULT '[]',
  template_name text NOT NULL,
  render_key text NOT NULL,
  colours jsonb NOT NULL DEFAULT '[]',
  output_id text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX order_items_order ON order_items (order_id, position);

-- §3.4, with `manual` quotes only in phase 1 (Ruling 1); the slice columns are nullable
-- and phase 2 fills them. `label` is the typed quote's one line's label.
CREATE TABLE order_item_quotes (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_item_id text NOT NULL REFERENCES order_items (id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  source text NOT NULL CHECK (source IN ('slice', 'manual')),
  accepted boolean NOT NULL DEFAULT false,
  sliced_sha256 text,
  sliced_blob_key text,
  plates jsonb NOT NULL DEFAULT '[]',
  lines jsonb NOT NULL DEFAULT '[]',
  unit_cost_minor bigint NOT NULL,
  unit_price_minor bigint NOT NULL,
  quantity integer NOT NULL CHECK (quantity >= 1),
  total_minor bigint NOT NULL,
  created_by jsonb NOT NULL
);
CREATE INDEX order_item_quotes_item ON order_item_quotes (order_item_id, created_at);
CREATE UNIQUE INDEX order_item_quotes_accepted ON order_item_quotes (order_item_id) WHERE accepted;

-- §6.1 by hand (Ruling 3): the columns a person writes; phase 3 adds the print path's.
CREATE TABLE order_item_fulfilments (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_item_id text NOT NULL REFERENCES order_items (id) ON DELETE CASCADE,
  attempt integer NOT NULL CHECK (attempt >= 1),
  quantity integer NOT NULL CHECK (quantity >= 0),
  outcome text NOT NULL CHECK (outcome IN ('succeeded', 'failed', 'partial', 'unknown')),
  failure_reason text,
  started_at timestamptz,
  finished_at timestamptz,
  notes text NOT NULL DEFAULT '',
  accepted_at timestamptz,
  created_by jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (order_item_id, attempt)
);

-- §6.3 (Ruling 2): an order may ship in parts.
CREATE TABLE order_shipments (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  carrier text,
  service text,
  tracking text,
  shipped_at timestamptz,
  address jsonb,
  cost_minor bigint,
  notes text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX order_shipments_order ON order_shipments (order_id, created_at);

CREATE TABLE order_shipment_items (
  shipment_id text NOT NULL REFERENCES order_shipments (id) ON DELETE CASCADE,
  order_item_id text NOT NULL REFERENCES order_items (id) ON DELETE CASCADE,
  quantity integer NOT NULL CHECK (quantity >= 1),
  PRIMARY KEY (shipment_id, order_item_id)
);
