-- A slice quote's settings per filament slot (#5571, `SliceQuoteRequest.part_settings`),
-- as `bambuddy/part_settings.py` `dumped` writes them: `{"<slot>": {"<key>": value}}`.
-- Null for a quote sliced with none, and for every quote recorded before. An item's print
-- takes the accepted quote's when its request sends none, so it prints what was priced.
ALTER TABLE order_item_quotes ADD COLUMN part_settings jsonb;
