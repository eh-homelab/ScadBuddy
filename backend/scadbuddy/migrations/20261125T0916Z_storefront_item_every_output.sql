-- #5701 PR 3 (spec 2026-11-17 §7.2, §7.4): whether a cart line's `model_sha256` is known to be
-- of its whole render. A pipeline may make several outputs, and a price key is of one 3MF, so
-- a line of a pipeline listing has a firm price only when this is true. FALSE on a line added
-- before: a pipeline's line then kept its first output's 3MF, whatever else the render made.
-- `orders/storefront/line_prices.py` reads it for a pipeline listing's line only.
ALTER TABLE storefront_item_prices ADD COLUMN every_output boolean NOT NULL DEFAULT false;
