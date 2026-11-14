-- #6152: the filament a visitor picked for each colour parameter, kept with the visitor's
-- render row (the estimate prices each slot at it), and a listing's `one_material` switch.
ALTER TABLE storefront_renders ADD COLUMN picks jsonb NOT NULL DEFAULT '{}';
ALTER TABLE storefront_listings ADD COLUMN one_material boolean NOT NULL DEFAULT false;
