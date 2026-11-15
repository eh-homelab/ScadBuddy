-- #6133 (spec 2026-11-13 §5): a storefront customer's own models and the things the shop
-- buys for their project.
--
-- `storefront_uploads`: one row per presigned upload slot. The row owns its staging object
-- (`<prefix>/staging/<id>` in the S3 bucket): the sweep deletes the object, then the row.
-- `visitor_id` is SET NULL with the visitor, never CASCADE, so a swept visitor never
-- leaves an object no row names; the sweep takes a row with no visitor too.
CREATE TABLE storefront_uploads (
  id text PRIMARY KEY,
  visitor_id text REFERENCES storefront_visitors (id) ON DELETE SET NULL,
  object_key text NOT NULL UNIQUE,
  filename text NOT NULL,
  size bigint NOT NULL CHECK (size >= 1),
  media_type text NOT NULL CHECK (media_type IN ('model/stl', 'model/3mf')),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  claimed_at timestamptz
);

CREATE INDEX storefront_uploads_visitor ON storefront_uploads (visitor_id);
CREATE INDEX storefront_uploads_unclaimed ON storefront_uploads (expires_at)
  WHERE claimed_at IS NULL;

-- An own-model request item (`request_kind` own_model in the spec): a `request` item with
-- this row. A design request has none. `colour_preferences` is what the customer asked:
-- `{"shop_choice": true}` or `{"filaments": [<price book id>, ...]}`.
CREATE TABLE order_item_model_requests (
  item_id text PRIMARY KEY REFERENCES order_items (id) ON DELETE CASCADE,
  colour_preferences jsonb NOT NULL DEFAULT '{"shop_choice": true}'
);

-- One row per file or link of a request item. A file names its staging upload until it
-- is imported into the native library; then `upload_id` is null and `library_file_id` is
-- set, so no row names a staging key after import.
CREATE TABLE order_item_sources (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  item_id text NOT NULL REFERENCES order_items (id) ON DELETE CASCADE,
  position integer NOT NULL,
  kind text NOT NULL CHECK (kind IN ('file', 'link')),
  purpose text NOT NULL CHECK (purpose IN ('print', 'purchase')),
  upload_id text UNIQUE REFERENCES storefront_uploads (id) ON DELETE SET NULL,
  filename text,
  size bigint,
  media_type text,
  url text,
  link_type text CHECK (link_type IN ('makerworld', 'printables', 'thingiverse', 'manual')),
  canonical text,
  note text NOT NULL DEFAULT '',
  check_status text CHECK (check_status IN ('checking', 'ready', 'refused', 'failed')),
  check_reason text,
  facts jsonb,
  sha256 text,
  preview_key text,
  status text NOT NULL CHECK (
    status IN ('open', 'imported', 'manual', 'declined', 'pending', 'recorded', 'not_needed')
  ),
  status_reason text,
  library_file_id bigint,
  result_item_id text REFERENCES order_items (id) ON DELETE SET NULL,
  cost_entry_id text UNIQUE,
  charge_id text UNIQUE,
  shop_note text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (item_id, position),
  -- A file is a print source with a name, a size and a check; a link has a URL and a type.
  CONSTRAINT order_item_sources_file CHECK (
    kind <> 'file' OR (
      purpose = 'print' AND filename IS NOT NULL AND size IS NOT NULL
      AND media_type IS NOT NULL AND url IS NULL AND check_status IS NOT NULL
    )
  ),
  CONSTRAINT order_item_sources_link CHECK (
    kind <> 'link' OR (url IS NOT NULL AND link_type IS NOT NULL AND upload_id IS NULL)
  ),
  -- What the shop buys is always handled by hand.
  CONSTRAINT order_item_sources_purchase_manual CHECK (
    purpose <> 'purchase' OR link_type = 'manual'
  ),
  CONSTRAINT order_item_sources_status CHECK (
    (purpose = 'print' AND status IN ('open', 'imported', 'manual', 'declined'))
    OR (purpose = 'purchase' AND status IN ('pending', 'recorded', 'not_needed'))
  ),
  -- An imported source names its library file, and a file no staging upload.
  CONSTRAINT order_item_sources_imported CHECK (
    status <> 'imported' OR (library_file_id IS NOT NULL AND upload_id IS NULL)
  )
);

CREATE INDEX order_item_sources_order ON order_item_sources (order_id);
CREATE INDEX order_item_sources_pending_purchase ON order_item_sources (order_id)
  WHERE purpose = 'purchase' AND status = 'pending';
