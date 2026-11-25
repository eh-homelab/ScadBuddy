-- The timelapse scan was dropped (spec 2026-11-05 §9), so its stamp goes too (#7366).
-- The previous build never names this column (#7341), so the drop is safe during a roll.
ALTER TABLE order_item_fulfilments DROP COLUMN IF EXISTS timelapse_scanned_at;
