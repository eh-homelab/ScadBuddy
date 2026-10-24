-- Orders phase 3, attachments (#4251, spec 2026-10-18 §6.2): any file a person keeps on
-- an order or one of its line items (a customer's reference, a photo of a print's
-- problem, the finished object, the packed box, a timelapse, an invoice). The bytes are
-- a blob of ScadBuddy's own store (`attachment-<sha256>`, Ruling 8), held by a
-- `blob_refs` row `(attachment, <id>)` for as long as this row stands, so the blob
-- sweep never takes them. `subject` says what the file is attached to; `order_id` is
-- the order it belongs to either way, so an order's whole list is one index read.
CREATE TABLE attachments (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  subject text NOT NULL CHECK (subject ~ '^(order|order_item):[^:]+$'),
  kind text NOT NULL CHECK (kind IN ('image', 'video', 'document', 'other')),
  role text NOT NULL DEFAULT 'other'
    CHECK (role IN ('reference', 'issue', 'finished', 'packed', 'timelapse', 'other')),
  filename text NOT NULL CHECK (char_length(filename) BETWEEN 1 AND 255),
  media_type text NOT NULL,
  size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  blob_key text NOT NULL,
  caption text NOT NULL DEFAULT '' CHECK (char_length(caption) <= 1000),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  created_by jsonb NOT NULL
);
CREATE INDEX attachments_order ON attachments (order_id, created_at);
CREATE INDEX attachments_subject ON attachments (subject, created_at);

-- The image a person marked as what was actually delivered for the item: one of the
-- item's own image attachments. Removing that attachment clears the mark.
ALTER TABLE order_items
  ADD COLUMN delivered_attachment_id text REFERENCES attachments (id) ON DELETE SET NULL;
