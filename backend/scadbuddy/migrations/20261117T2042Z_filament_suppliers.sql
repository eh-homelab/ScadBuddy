-- Suppliers, their products and prices, invoices and the shopping list (#6266, epic
-- #6260, spec 2026-11-14-filament-management-design.md §5.8). Needs the spool types of
-- `20261114T1301Z_filament_spools.sql`. The tables are created in dependency order:
-- `filament_price_points` points at `filament_invoice_lines`, so it comes after them.
-- Money is `numeric(12,4)`, never float.

-- §5.8 A supplier: a shop a person buys from. A name is unique among the unarchived.
CREATE TABLE filament_suppliers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    website text,
    customer_number text,
    note text,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX filament_suppliers_name ON filament_suppliers (upper(name))
    WHERE archived_at IS NULL;

-- §5.8 A supplier product: one SKU of one supplier, for one spool type. `pack_size` is
-- how many spools one SKU holds.
CREATE TABLE filament_supplier_products (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id uuid NOT NULL REFERENCES filament_suppliers (id),
    type_id uuid NOT NULL REFERENCES filament_spool_types (id),
    sku text NOT NULL,
    product_url text,
    pack_size integer NOT NULL DEFAULT 1 CHECK (pack_size > 0),
    note text,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX filament_supplier_products_sku
    ON filament_supplier_products (supplier_id, upper(sku)) WHERE archived_at IS NULL;
CREATE INDEX filament_supplier_products_type ON filament_supplier_products (type_id);

-- §5.8 An invoice: a draft until a person confirms it. The four `file_*` columns hold
-- the scan or photo, all set or all null. A number is unique per supplier;
-- `import_run_id` makes a repeated import answer with the first invoice.
CREATE TABLE filament_invoices (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    supplier_id uuid NOT NULL REFERENCES filament_suppliers (id),
    number text,
    invoice_date date NOT NULL,
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    subtotal numeric(12,4) NOT NULL CHECK (subtotal >= 0),
    shipping numeric(12,4) NOT NULL DEFAULT 0 CHECK (shipping >= 0),
    tax numeric(12,4) NOT NULL DEFAULT 0 CHECK (tax >= 0),
    total numeric(12,4) NOT NULL CHECK (total >= 0),
    file_key text,
    file_name text,
    file_media_type text,
    file_size bigint,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed')),
    import_run_id text,
    confirmed_at timestamptz,
    note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT filament_invoices_file_whole CHECK (
        (file_key IS NULL) = (file_name IS NULL)
        AND (file_key IS NULL) = (file_media_type IS NULL)
        AND (file_key IS NULL) = (file_size IS NULL)
    )
);
CREATE UNIQUE INDEX filament_invoices_number ON filament_invoices (supplier_id, upper(number))
    WHERE number IS NOT NULL;
CREATE UNIQUE INDEX filament_invoices_import_run ON filament_invoices (import_run_id)
    WHERE import_run_id IS NOT NULL;
CREATE INDEX filament_invoices_supplier ON filament_invoices (supplier_id);

-- §5.8 An invoice line. `line_total` is the subtotal plus its tax; `allocated_cost` adds
-- its share of the shipping. `update_spool_ids` are existing spools the line fills in at
-- confirm, never more than the line's quantity.
CREATE TABLE filament_invoice_lines (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id uuid NOT NULL REFERENCES filament_invoices (id) ON DELETE CASCADE,
    position integer NOT NULL,
    description text NOT NULL,
    sku text,
    quantity integer NOT NULL CHECK (quantity > 0),
    unit_price numeric(12,4) NOT NULL CHECK (unit_price >= 0),
    line_subtotal numeric(12,4) NOT NULL CHECK (line_subtotal >= 0),
    line_tax numeric(12,4) NOT NULL CHECK (line_tax >= 0),
    line_total numeric(12,4) NOT NULL CHECK (line_total >= 0),
    allocated_cost numeric(12,4) NOT NULL CHECK (allocated_cost >= 0),
    tax_basis text NOT NULL DEFAULT 'none',
    colour_code text,
    type_id uuid REFERENCES filament_spool_types (id),
    supplier_product_id uuid REFERENCES filament_supplier_products (id),
    proposed_type jsonb,
    update_spool_ids uuid[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT filament_invoice_lines_position UNIQUE (invoice_id, position),
    CONSTRAINT filament_invoice_lines_update_ids
        CHECK (cardinality(update_spool_ids) <= quantity)
);

-- §5.8 A price point: what a product cost on a day, per pack, with tax. One of source
-- `invoice` names the line it came from; one of source `manual` names none.
CREATE TABLE filament_price_points (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id uuid NOT NULL REFERENCES filament_supplier_products (id),
    price numeric(12,4) NOT NULL CHECK (price >= 0),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    on_date date NOT NULL,
    source text NOT NULL CHECK (source IN ('invoice', 'manual')),
    invoice_line_id uuid REFERENCES filament_invoice_lines (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT filament_price_points_source_line
        CHECK ((source = 'invoice') = (invoice_line_id IS NOT NULL))
);
CREATE UNIQUE INDEX filament_price_points_line ON filament_price_points (invoice_line_id)
    WHERE invoice_line_id IS NOT NULL;
CREATE INDEX filament_price_points_product ON filament_price_points (product_id, on_date DESC);

-- §5.8 The shopping list: what a person means to buy. `type_id` is null for a wish with
-- no type yet; `material` is then what it names.
CREATE TABLE filament_shopping_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    type_id uuid REFERENCES filament_spool_types (id),
    material text NOT NULL,
    subtype text,
    brand text,
    colour_name text,
    quantity_spools integer NOT NULL DEFAULT 1 CHECK (quantity_spools > 0),
    note text,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'purchased')),
    purchased_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- §5.8 The line a spool was bought on.
ALTER TABLE filament_spools
    ADD COLUMN invoice_line_id uuid REFERENCES filament_invoice_lines (id);
CREATE INDEX filament_spools_invoice_line ON filament_spools (invoice_line_id)
    WHERE invoice_line_id IS NOT NULL;
