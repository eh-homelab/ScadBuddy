-- Orders phase 2 (#4250, spec 2026-10-18 §5.1; plan 2026-10-22 Ruling 4): the price
-- book's filaments, ScadBuddy's own records of what a kilogram of a filament costs.
-- A record prices and never slices. The identity is `_filament_key`'s grouping
-- (material, subtype, brand, colour; `bambuddy/farm.py`), upper-cased, so phase 5 can
-- match a record to Bambuddy's spools by it; every column of the index is non-null or
-- coalesced, so two records can never both pass it. `archived_at` hides a record from
-- the book and the pickers; a quote already priced with it keeps its lines (§5.1), and
-- the index is over the records in the book only, so an archived record frees its
-- identity for a new one (there is no unarchive): phase 5's matcher takes the record
-- that is not archived.

CREATE TABLE filaments (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  material text NOT NULL,
  subtype text,
  brand text,
  colour_name text,
  colour text NOT NULL CHECK (colour ~ '^#[0-9A-Fa-f]{6}$'),
  cost_per_kg numeric(12,4) NOT NULL CHECK (cost_per_kg >= 0),
  density_g_cm3 numeric(6,3) CHECK (density_g_cm3 > 0),
  archived_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX filaments_identity ON filaments (
  upper(material), upper(coalesce(subtype, '')), upper(coalesce(brand, '')), upper(colour)
) WHERE archived_at IS NULL;
