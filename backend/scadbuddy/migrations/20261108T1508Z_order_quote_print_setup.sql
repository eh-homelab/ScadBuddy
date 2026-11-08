-- A slice quote's whole print setup (#4673, order operations spec §5.1), as it sliced:
-- `choices` holds `printer_id`, `printer`, `path`, `choices` (`PrintChoices`), `prime_tower`,
-- `supports` and `print_sequence`, each in `PrintRunRequest`'s shape (`orders/quote.py`
-- `print_setup`); `filament_plan` the spool per slot it sliced with. Null for a manual or
-- archive quote, which sliced nothing, and for every quote recorded before.
ALTER TABLE order_item_quotes ADD COLUMN choices jsonb, ADD COLUMN filament_plan jsonb;
