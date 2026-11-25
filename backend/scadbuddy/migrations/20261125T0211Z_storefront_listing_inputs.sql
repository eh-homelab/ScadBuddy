-- #5701 (spec 2026-11-17 §4.2): a pipeline template's own inputs on its listing, without
-- `params` and `v` ('{}' for every other listing), and the inputs version they were made
-- at (null: the commit's own INPUTS_VERSION). What publish settles of them is in the
-- `snapshot` column.
ALTER TABLE storefront_listings
  ADD COLUMN inputs jsonb NOT NULL DEFAULT '{}',
  ADD COLUMN inputs_v integer;
