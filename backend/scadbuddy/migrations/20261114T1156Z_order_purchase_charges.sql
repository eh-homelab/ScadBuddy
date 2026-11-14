-- #6133 (spec 2026-11-13 §5.5, §10): what the shop buys for a customer's project.
--
-- A `purchase` charge is what the customer pays for one thing the shop bought: it names
-- its purchase link (`order_item_sources`, purpose `purchase`), exactly one charge per
-- link, and no other kind names one. A `handling` charge is the shop's fee for buying,
-- at most one per order. Only the lifecycle writes either kind
-- (`Lifecycle.record_purchase`, `update_purchase`, `decline_purchase`); a charges replace
-- keeps them.
ALTER TABLE order_charges
  DROP CONSTRAINT order_charges_kind_check,
  ADD CONSTRAINT order_charges_kind_check
    CHECK (kind IN ('shipping', 'discount', 'fee', 'tax', 'purchase', 'handling')),
  ADD COLUMN purchase_link_id text UNIQUE
    REFERENCES order_item_sources (id) ON DELETE CASCADE,
  ADD CONSTRAINT order_charges_purchase_link
    CHECK ((kind = 'purchase') = (purchase_link_id IS NOT NULL));

CREATE UNIQUE INDEX order_charges_handling ON order_charges (order_id)
  WHERE kind = 'handling';
