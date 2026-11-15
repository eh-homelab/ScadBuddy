-- #5660 (spec 2026-11-08-order-notify-customer-design.md §7.1): each message the shop sent,
-- or tried to send, to an order's customer through a messaging plugin. The agent sends;
-- the API keeps the record (`orders/messages.py`), each write with its `order_events` row.
-- A row is claimed `sending` before the plugin is called, so a retry under the same key
-- never sends twice.
CREATE TABLE order_messages (
  id text PRIMARY KEY DEFAULT gen_random_uuid()::text,
  order_id text NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
  template text NOT NULL CHECK (template IN ('quoted', 'accepted', 'ready', 'shipped')),
  -- The binding used: `remote:<plugin>` or `package:<package>/<server>`, and its tool.
  source text NOT NULL,
  tool text NOT NULL,
  recipients jsonb NOT NULL,
  -- Null for a channel with no subject (SMS).
  subject text,
  text text NOT NULL,
  status text NOT NULL CHECK (status IN ('sending', 'sent', 'failed', 'unknown', 'skipped')),
  -- A fixed category, never the plugin's own text.
  reason text,
  provider_message_id text,
  idempotency_key text NOT NULL UNIQUE,
  -- The order's newest status change (`order_status_changes.id`) when it was claimed:
  -- one notice of a template per status change unless a person sends it again.
  episode bigint,
  resend_of text REFERENCES order_messages (id),
  by jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  finished_at timestamptz
);
CREATE INDEX order_messages_order ON order_messages (order_id, created_at);
