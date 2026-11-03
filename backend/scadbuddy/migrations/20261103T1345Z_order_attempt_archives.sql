-- #4253 (spec 2026-10-18 §8.2, §6.1): what an attempt's Bambuddy archives say of its print,
-- copied once the print settled. `archive_ids` are the archives its run's queue items made
-- (`print_sends` by `run_id`, then `print_links`), one per plate, oldest first;
-- `actual_seconds` and `actual_grams` are their sums (Bambuddy's `actual_time_seconds`, and
-- `total_filament_actual_grams` else `filament_used_grams`). Empty and null on an attempt
-- recorded by hand, or one whose archives Bambuddy has not linked yet.
ALTER TABLE order_item_fulfilments
  ADD COLUMN archive_ids integer[] NOT NULL DEFAULT '{}',
  ADD COLUMN actual_seconds integer,
  ADD COLUMN actual_grams double precision;
