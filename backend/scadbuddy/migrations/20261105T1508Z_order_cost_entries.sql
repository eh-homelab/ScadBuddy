-- #5326, job cost spec 2026-11-05 §4.3 and §4.4: the time and the other costs a job took,
-- on the order (`order_item_id` null) or one of its items. Written only through the
-- lifecycle, in any order status, each with its `order_events` row. A cost is computed on
-- read from these and the attempts; nothing here stores a cost or a margin.
CREATE TABLE order_time_entries (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  -- An item with entries is never deleted (`Lifecycle.delete_item`); it is cancelled.
  order_item_id text REFERENCES order_items (id),
  kind text NOT NULL
    CHECK (kind IN ('design', 'setup', 'post_processing', 'packing', 'other')),
  minutes integer NOT NULL CHECK (minutes > 0),
  -- The labour rate when it was written, so a later rate change never rewrites it; null
  -- when the order's currency was not the shop's and no rate could be copied.
  rate_minor_per_hour bigint CHECK (rate_minor_per_hour >= 0),
  note text NOT NULL DEFAULT '',
  started_at timestamptz,
  -- What wrote it (`timer:<item or order>:<started_at>`); null for an entry typed by hand.
  source text,
  created_by jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX order_time_entries_order ON order_time_entries (order_id, created_at);
-- A retry whose first reply was lost writes once.
CREATE UNIQUE INDEX order_time_entries_source
  ON order_time_entries (order_id, kind, source) WHERE source IS NOT NULL;

CREATE TABLE order_cost_entries (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  -- An item with entries is never deleted (`Lifecycle.delete_item`); it is cancelled.
  order_item_id text REFERENCES order_items (id),
  kind text NOT NULL CHECK (kind IN ('assistant', 'payment_fee', 'material', 'other')),
  amount_minor bigint NOT NULL CHECK (amount_minor > 0),
  note text NOT NULL DEFAULT '',
  -- What wrote it (`ai_session:<id>`, `payment:<id>`); null for an entry by hand.
  source text,
  created_by jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX order_cost_entries_order ON order_cost_entries (order_id, created_at);
-- A writer that runs twice (a replayed webhook, a retried flow) writes once.
CREATE UNIQUE INDEX order_cost_entries_source
  ON order_cost_entries (order_id, kind, source) WHERE source IS NOT NULL;
