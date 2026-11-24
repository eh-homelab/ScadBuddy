-- #7026, inventory spec §6.3a: arbitrary key-value properties on any entity, one row per
-- entity and key, cascading from `entities` like links. A value is a string, a number, a
-- boolean or a list of strings (the store checks the list's items and the limits). A
-- kind's schema documents and types the keys it knows and never refuses a key; a seeded
-- row's `updated_by` is NULL (no `Actor` kind is a migration), a person's write sets it.
CREATE TABLE entity_properties (
  entity_id uuid NOT NULL REFERENCES entities (id) ON DELETE CASCADE,
  key text NOT NULL CHECK (key ~ '^[a-z][a-z0-9_]{0,63}$'),
  value jsonb NOT NULL CHECK (jsonb_typeof(value) IN ('string', 'number', 'boolean', 'array')),
  source text NOT NULL DEFAULT 'manual',
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_by jsonb NOT NULL,
  PRIMARY KEY (entity_id, key)
);
CREATE INDEX entity_properties_key ON entity_properties (key);

CREATE TABLE entity_property_schemas (
  kind text PRIMARY KEY CHECK (kind ~ '^[a-z_]+$'),
  schema jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  updated_by jsonb
);

INSERT INTO entity_property_schemas (kind, schema) VALUES
(
  'spool_type',
  '{
    "type": "object",
    "properties": {
      "mpn": {"type": "string", "description": "Manufacturer part number"},
      "gtin": {
        "type": "array",
        "items": {"type": "string"},
        "description": "GTIN / EAN / UPC barcodes (spool and refill)"
      },
      "asin": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Amazon ASINs as `marketplace:ASIN`, such as `us:B0CXYZ1234`"
      },
      "manufacturer_url": {"type": "string", "description": "The manufacturer''s product page"},
      "empty_spool_weight_g": {
        "type": "number",
        "minimum": 0,
        "description": "Weight of the empty spool in grams"
      },
      "td": {"type": "number", "minimum": 0, "description": "Transmission distance (HueForge TD)"}
    }
  }'
),
(
  'spool',
  '{
    "type": "object",
    "properties": {
      "lot_number": {"type": "string", "description": "Lot or batch number printed on the label"}
    }
  }'
);
