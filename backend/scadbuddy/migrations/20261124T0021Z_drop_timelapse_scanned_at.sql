-- #7272: the timelapse scan was dropped (#7235), so nothing writes this column.
ALTER TABLE order_item_fulfilments DROP COLUMN timelapse_scanned_at;
