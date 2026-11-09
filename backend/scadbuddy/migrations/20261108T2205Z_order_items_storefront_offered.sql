-- #5282 (storefront phase 3, plan Task 8): the names of the parameters a storefront
-- listing offered when a visitor added the item. The visitor's draft shows only these;
-- every other value (the listing's locks and the template's defaults) stays the shop's.
-- Null on every item not added from a listing.
ALTER TABLE order_items ADD COLUMN storefront_offered text[];
