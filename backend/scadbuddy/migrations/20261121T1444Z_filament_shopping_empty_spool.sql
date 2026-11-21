-- #6868: an empty spool adds its type to the shopping list. `empty_spool_id` is the spool
-- whose move into empty (its grams reached 0, or it was archived) added the item; null for
-- an item a person added. A deleted spool leaves the item, with no spool named.
ALTER TABLE filament_shopping_items
    ADD COLUMN empty_spool_id uuid REFERENCES filament_spools (id) ON DELETE SET NULL;
CREATE INDEX filament_shopping_items_pending_type ON filament_shopping_items (type_id)
    WHERE status = 'pending';
