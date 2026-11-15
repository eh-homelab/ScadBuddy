-- #6258: a listing's allowed materials, in the shop's order of preference (empty is the
-- shop's default, `storefront_materials`), and the material a visitor's render resolved
-- to (the estimate prices each slot at that material's filament of its colour).
--
-- #6178's `storefront_listings.one_material` and `storefront_renders.picks` stay, and no
-- code reads or writes them now. While this build rolls out, the API pod of the build
-- before it still names them in its SELECTs and INSERTs. A later migration drops them.
ALTER TABLE storefront_listings ADD COLUMN materials text[] NOT NULL DEFAULT '{}';
ALTER TABLE storefront_renders ADD COLUMN material text;
