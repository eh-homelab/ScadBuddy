-- The inventory item core (#7067, epic #6949; spec
-- 2026-11-21-inventory-and-purchasing-design.md §6.4-6.6): kinds, items, supplier offers
-- and their price points, lots, units and stock moves. Phase 2 changes no filament table;
-- it only adds foreign keys to `filament_suppliers`, `filament_locations` and
-- `filament_invoice_lines` (§6.5). Stock money is in the shop currency (`orders_currency`),
-- in minor units, unrounded (`numeric(20,6)`), as `parts.unit_cost_minor` keeps a fraction.

-- §6.4 A kind: a tree of what things are. `tracking` says how its items are counted:
-- `lot` (counted stock), `unit` (one by one), `filament` (spools with a gram ledger), or
-- `none` (not counted: offers and prices, never lots, units or moves; the owner's
-- amendment to §6.4 of 2026-11-23). `attribute_schema` documents and types the items'
-- properties and never restricts them (§6.3a); a child kind inherits its parent's keys.
CREATE TABLE item_kinds (
    id text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9_]{0,39}$'),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    parent_id text REFERENCES item_kinds (id),
    tracking text NOT NULL CHECK (tracking IN ('lot', 'unit', 'filament', 'none')),
    uom text NOT NULL DEFAULT 'each' CHECK (uom ~ '^[a-z]{1,8}$'),
    attribute_schema jsonb NOT NULL DEFAULT '{"type": "object", "properties": {}}',
    life_basis text NOT NULL DEFAULT 'none' CHECK (life_basis IN ('none', 'hours', 'months')),
    saleable boolean NOT NULL DEFAULT false,
    builtin boolean NOT NULL DEFAULT false,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (parent_id IS DISTINCT FROM id)
);

-- §6.4 An item: what a thing is, the catalog entry. Its attributes are entity
-- properties, not columns. `saleable` null takes the kind's. `edited_fields` are the
-- fields a person changed, which an import skips.
CREATE TABLE items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind_id text NOT NULL REFERENCES item_kinds (id),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    manufacturer text NOT NULL DEFAULT '',
    compatible_with text[] NOT NULL DEFAULT '{}',
    low_stock numeric(20,6) NOT NULL DEFAULT 0 CHECK (low_stock >= 0),
    life_hours numeric(12,2) CHECK (life_hours > 0),
    life_months integer CHECK (life_months > 0),
    saleable boolean,
    source text NOT NULL DEFAULT 'manual',
    source_ref text CHECK (source_ref <> ''),
    edited_fields text[] NOT NULL DEFAULT '{}',
    note text NOT NULL DEFAULT '' CHECK (length(note) <= 4000),
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX items_kind_name ON items (kind_id, lower(name)) WHERE archived_at IS NULL;
CREATE UNIQUE INDEX items_source_ref ON items (source, source_ref) WHERE source_ref IS NOT NULL;

-- §6.5 An offer: one item at one supplier. A SKU is unique per supplier and item, not per
-- supplier (D13): one SKU may cover several items, such as a bundle. Offers are for
-- non-filament items until phase 3 (D12).
CREATE TABLE supplier_offers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id uuid NOT NULL REFERENCES filament_suppliers (id),
    item_id uuid NOT NULL REFERENCES items (id),
    sku text NOT NULL CHECK (length(sku) BETWEEN 1 AND 100),
    url text CHECK (url ~* '^https?://'),
    pack_size numeric(20,6) NOT NULL DEFAULT 1 CHECK (pack_size > 0),
    pack_uom text NOT NULL DEFAULT 'each',
    note text,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX supplier_offers_sku ON supplier_offers (supplier_id, upper(sku), item_id)
    WHERE archived_at IS NULL;
CREATE INDEX supplier_offers_item ON supplier_offers (item_id);

-- §6.5 A pack price of an offer, in any ISO currency, in the final shape that
-- `filament_price_points` moves into in phase 3 (the plan's Ruling 4). An invoice price
-- names its invoice line; a manual one names none.
CREATE TABLE offer_price_points (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id uuid NOT NULL REFERENCES supplier_offers (id),
    price numeric(12,4) NOT NULL CHECK (price >= 0),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    on_date date NOT NULL,
    source text NOT NULL CHECK (source IN ('invoice', 'manual')),
    invoice_line_id uuid REFERENCES filament_invoice_lines (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT offer_price_points_source_line CHECK ((source = 'invoice') = (invoice_line_id IS NOT NULL))
);
CREATE INDEX offer_price_points_offer ON offer_price_points (offer_id, on_date DESC);

-- §6.4 A lot of a `lot` item. `on_hand` is derived from its moves and kept for reads; it
-- may go below zero (§6.6). `origin = 'manual'` is a receive with no invoice yet.
-- `cost_currency` is set on the opening lots made from parts, and on a lot received at a
-- cost in a currency other than the shop's, which opens with its cost unknown
-- (R-7067-11). `foreign_unit_cost_minor` keeps that cost, one unit's in minor units of
-- `cost_currency`, so the amount a person gave is not lost (R-P2-11).
CREATE TABLE stock_lots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id uuid NOT NULL REFERENCES items (id),
    location_id uuid REFERENCES filament_locations (id),
    received_qty numeric(20,6) NOT NULL,
    on_hand numeric(20,6) NOT NULL,
    unit_cost_minor numeric(20,6) NOT NULL CHECK (unit_cost_minor >= 0),
    cost_unknown boolean NOT NULL DEFAULT false,
    cost_currency text CHECK (cost_currency ~ '^[A-Z]{3}$'),
    foreign_unit_cost_minor numeric(20,6) CHECK (foreign_unit_cost_minor >= 0),
    origin text NOT NULL
        CHECK (origin IN ('invoice_line', 'production_run', 'opening', 'adjustment', 'manual')),
    invoice_line_id uuid REFERENCES filament_invoice_lines (id),
    production_run_id uuid,
    note text NOT NULL DEFAULT '',
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by jsonb NOT NULL,
    CONSTRAINT stock_lots_foreign_cost CHECK (foreign_unit_cost_minor IS NULL OR cost_currency IS NOT NULL)
);
-- FIFO order (D2).
CREATE INDEX stock_lots_fifo ON stock_lots (item_id, received_at, id);

-- §6.4 A unit of a `unit` item, one by one. `installed_in_unit_id` is the unit it is
-- fitted in (a nozzle in a printer); `printer_id` is set on a unit that is a printer, and
-- `location_ref_id` on one that is a place (a dry bin). Install is phase 4 (#7069).
CREATE TABLE stock_units (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id uuid NOT NULL REFERENCES items (id),
    serial text CHECK (serial <> ''),
    name text NOT NULL DEFAULT '',
    location_id uuid REFERENCES filament_locations (id),
    status text NOT NULL DEFAULT 'in_stock'
        CHECK (status IN ('in_stock', 'in_service', 'retired', 'scrapped', 'sold')),
    installed_in_unit_id uuid REFERENCES stock_units (id)
        CHECK (installed_in_unit_id IS DISTINCT FROM id),
    cost_minor numeric(20,6) NOT NULL CHECK (cost_minor >= 0),
    cost_unknown boolean NOT NULL DEFAULT false,
    origin text NOT NULL
        CHECK (origin IN ('invoice_line', 'production_run', 'opening', 'adjustment', 'manual')),
    invoice_line_id uuid REFERENCES filament_invoice_lines (id),
    production_run_id uuid,
    in_service_at timestamptz,
    retired_at timestamptz,
    life_hours numeric(12,2) CHECK (life_hours > 0),
    life_months integer CHECK (life_months > 0),
    printer_id uuid REFERENCES printers (id) ON DELETE SET NULL,
    location_ref_id uuid REFERENCES filament_locations (id),
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by jsonb NOT NULL
);
CREATE UNIQUE INDEX stock_units_serial ON stock_units (item_id, serial) WHERE serial IS NOT NULL;
CREATE INDEX stock_units_item ON stock_units (item_id);

-- §6.6 A move of stock, on one lot or one unit. `qty` is signed. Its cost is a snapshot
-- taken when it is written (D14): a later lot cost change moves no past move. A shortfall
-- move takes the newest lot's cost and sets `needs_review`. Only a return names a move it
-- reverses, and each move is reversed at most once. `order_id` and `order_item_id` have no foreign key: a move
-- outlives a deleted draft, as `part_stock_moves` did.
CREATE TABLE stock_moves (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    item_id uuid NOT NULL REFERENCES items (id),
    lot_id uuid REFERENCES stock_lots (id),
    unit_id uuid REFERENCES stock_units (id),
    kind text NOT NULL CHECK (kind IN (
        'receive', 'use', 'return', 'adjust', 'produce', 'transfer', 'sell', 'retire', 'scrap'
    )),
    qty numeric(20,6) NOT NULL CHECK (qty <> 0),
    unit_cost_minor numeric(20,6) NOT NULL CHECK (unit_cost_minor >= 0),
    amount_minor numeric(20,6) NOT NULL,
    cost_unknown boolean NOT NULL DEFAULT false,
    needs_review boolean NOT NULL DEFAULT false,
    reverses_move_id uuid UNIQUE REFERENCES stock_moves (id),
    reason text NOT NULL DEFAULT '',
    order_id text,
    order_item_id text,
    production_run_id uuid,
    print_run_id text,
    entity_id uuid REFERENCES entities (id),
    actor jsonb NOT NULL,
    CONSTRAINT stock_moves_one_holder CHECK ((lot_id IS NULL) <> (unit_id IS NULL)),
    CONSTRAINT stock_moves_reverses_return CHECK (reverses_move_id IS NULL OR kind = 'return'),
    -- Rounded to the column's six places: a fractional quantity at a fractional cost has
    -- up to twelve, and `amount_minor` stores six.
    CONSTRAINT stock_moves_amount CHECK (amount_minor = round(qty * unit_cost_minor, 6))
);
CREATE INDEX stock_moves_item ON stock_moves (item_id, at);
CREATE INDEX stock_moves_order_item ON stock_moves (order_item_id) WHERE order_item_id IS NOT NULL;
CREATE INDEX stock_moves_lot ON stock_moves (lot_id);
CREATE INDEX stock_moves_unit ON stock_moves (unit_id) WHERE unit_id IS NOT NULL;
-- The entities sweep skips an entity that a move names (R-P2-4).
CREATE INDEX stock_moves_entity ON stock_moves (entity_id) WHERE entity_id IS NOT NULL;

-- §6.4 The built-in kinds (the plan's Ruling 1). People add more.
INSERT INTO item_kinds (id, name, parent_id, tracking, uom, life_basis, saleable, builtin) VALUES
    ('filament', 'Filament', NULL, 'filament', 'g', 'none', false, true),
    ('printer', 'Printer', NULL, 'unit', 'each', 'hours', false, true),
    ('part', 'Part', NULL, 'lot', 'each', 'none', false, true),
    ('nozzle', 'Nozzle', 'part', 'unit', 'each', 'hours', false, true),
    ('hotend', 'Hotend', 'part', 'unit', 'each', 'hours', false, true),
    ('belt', 'Belt', 'part', 'unit', 'each', 'hours', false, true),
    ('build_plate', 'Build plate', 'part', 'unit', 'each', 'months', false, true),
    ('ams_part', 'AMS part', 'part', 'lot', 'each', 'none', false, true),
    ('hardware', 'Hardware', 'part', 'lot', 'each', 'none', false, true),
    ('consumable', 'Consumable', NULL, 'lot', 'each', 'none', false, true),
    ('desiccant', 'Desiccant', 'consumable', 'lot', 'g', 'none', false, true),
    ('packaging', 'Packaging', 'consumable', 'lot', 'each', 'none', false, true),
    ('lubricant', 'Lubricant', 'consumable', 'lot', 'ml', 'none', false, true),
    ('equipment', 'Equipment', NULL, 'unit', 'each', 'months', false, true),
    ('dry_bin', 'Dry bin', 'equipment', 'unit', 'each', 'months', false, true),
    ('tool', 'Tool', 'equipment', 'unit', 'each', 'months', false, true),
    ('printed_part', 'Printed part', NULL, 'lot', 'each', 'none', true, true),
    ('assembly', 'Assembly', NULL, 'lot', 'each', 'none', true, true),
    ('service', 'Service', NULL, 'none', 'each', 'none', false, true),
    ('spool_part', 'Spool part', NULL, 'unit', 'each', 'none', false, true);

-- A spool part (spec 2026-11-23-spool-parts-design.md §5, #7278).
UPDATE item_kinds SET attribute_schema = '{
    "type": "object",
    "properties": {
        "empty_weight_g": {
            "type": "integer",
            "exclusiveMinimum": 0,
            "description": "Weight of the empty part in grams"
        },
        "role": {
            "type": "string",
            "enum": ["shell", "addon"],
            "description": "A reusable shell, or an addon such as an AMS adaptor"
        }
    }
}'
WHERE id = 'spool_part';
