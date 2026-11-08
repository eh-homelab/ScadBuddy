-- A past print as an order item (#4777, order operations spec 2026-11-07 §3.2).
--
-- `origin_archive_ids`: the Bambuddy archives an item was taken from (the picked
-- archive and the other plates of its print run that ScadBuddy linked), for the
-- item's source chip and its `archive` quote. Empty for an item from any other source.
ALTER TABLE order_items ADD COLUMN origin_archive_ids integer[] NOT NULL DEFAULT '{}';

-- A quote priced from what a past print used (grams per slot as each plate archive
-- says, its actual seconds), with no re-slice.
ALTER TABLE order_item_quotes DROP CONSTRAINT order_item_quotes_source_check;
ALTER TABLE order_item_quotes ADD CONSTRAINT order_item_quotes_source_check
  CHECK (source IN ('slice', 'manual', 'archive'));

-- An archive no ScadBuddy output or library file printed has its file copied into the
-- native library once: picking the same archive again reuses the copy while the
-- library file is there (not trashed).
CREATE TABLE archive_library_copies (
  archive_id integer PRIMARY KEY,
  library_file_id bigint NOT NULL,
  copied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
