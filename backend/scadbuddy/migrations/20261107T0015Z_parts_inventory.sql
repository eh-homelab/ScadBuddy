-- #5543, job cost spec 2026-11-05 §11: the parts an item uses beside its filament (keychain
-- hooks, rings, magnets), their stock, and the rule a model gives its items.

-- A part, in the currency it was bought in (the shop's `orders_currency` when it was made).
-- Never deleted: a rule, an item and the stock moves name it.
CREATE TABLE parts (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
  supplier text NOT NULL DEFAULT '',
  link text NOT NULL DEFAULT '',
  currency text NOT NULL,
  -- The pack the next "Add stock" records: how many units, and its price with tax.
  pack_size integer NOT NULL CHECK (pack_size > 0),
  pack_price_minor bigint NOT NULL CHECK (pack_price_minor >= 0),
  -- One unit's cost in minor units: a rate, not an amount, so it keeps its fraction (a pack
  -- of 100 for 599 is 5.99 each) and a weighted average does not drift. Each cost entry
  -- made from it is rounded once.
  unit_cost_minor numeric(20, 6) NOT NULL CHECK (unit_cost_minor >= 0),
  -- Below zero when more was used than recorded: stock never blocks.
  on_hand integer NOT NULL DEFAULT 0,
  -- At or below this the part is low; 0 never warns.
  low_stock integer NOT NULL DEFAULT 0 CHECK (low_stock >= 0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX parts_name ON parts (lower(name));

-- Every change to a part's stock, written in the transaction that made it.
CREATE TABLE part_stock_moves (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  part_id text NOT NULL REFERENCES parts (id),
  kind text NOT NULL CHECK (kind IN ('add', 'adjust', 'use', 'return')),
  delta integer NOT NULL,
  on_hand_after integer NOT NULL,
  unit_cost_after_minor numeric(20, 6) NOT NULL,
  -- `add`: what the packs cost; `use`/`return`: the cost entry's amount; else null.
  amount_minor bigint,
  note text NOT NULL DEFAULT '',
  -- `use`/`return`: the item whose fulfilment took or gave back the parts. No foreign key:
  -- the line outlives a deleted draft order.
  order_id text,
  order_item_id text,
  created_by jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX part_stock_moves_part ON part_stock_moves (part_id, created_at);

-- What each unit of a model's items uses (`[{per_unit, part_id, when: [{colour, part_id}]}]`):
-- a new item of the slug copies it into `order_item_parts`.
CREATE TABLE model_part_rules (
  slug text PRIMARY KEY,
  lines jsonb NOT NULL,
  updated_by jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- The parts one unit of an item uses, and how many its fulfilment took out of stock (0 until
-- it is fulfilled, and again once unfulfilled), so an unfulfil gives back what was taken.
CREATE TABLE order_item_parts (
  order_item_id text NOT NULL REFERENCES order_items (id) ON DELETE CASCADE,
  part_id text NOT NULL REFERENCES parts (id),
  per_unit integer NOT NULL CHECK (per_unit BETWEEN 1 AND 1000),
  used integer NOT NULL DEFAULT 0 CHECK (used >= 0),
  PRIMARY KEY (order_item_id, part_id)
);
CREATE INDEX order_item_parts_part ON order_item_parts (part_id);
