-- #5282 (storefront phase 3, spec 2026-11-02 §5.2, §5.4, §6.1): a visitor's cart is an
-- order with `source = 'storefront'` and no `submitted_at`. It has no customer and no
-- number until it is submitted; every other order still needs both.
-- A visitor row keeps only the sha256 (hex) of the visitor id the storefront edge mints.
CREATE TABLE storefront_visitors (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  token_sha256 text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE orders
  ADD COLUMN source text NOT NULL DEFAULT 'back_office'
    CHECK (source IN ('back_office', 'storefront')),
  ADD COLUMN submitted_at timestamptz,
  ADD COLUMN storefront_visitor_id text REFERENCES storefront_visitors (id) ON DELETE SET NULL,
  ALTER COLUMN number DROP NOT NULL,
  ALTER COLUMN customer_id DROP NOT NULL,
  ADD CONSTRAINT orders_customer_and_number CHECK (
    (customer_id IS NOT NULL AND number IS NOT NULL)
    OR (source = 'storefront' AND submitted_at IS NULL)
  );

-- One draft per visitor: a second insert fails, and `Lifecycle.ensure_draft` reads the
-- winner.
CREATE UNIQUE INDEX orders_one_draft_per_visitor ON orders (storefront_visitor_id)
  WHERE submitted_at IS NULL AND source = 'storefront';
CREATE INDEX orders_source ON orders (source, submitted_at);
