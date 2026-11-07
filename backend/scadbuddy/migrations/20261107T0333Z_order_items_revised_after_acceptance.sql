-- #5350: an accepted item takes a new design revision after acceptance. Its accepted
-- quote stays accepted (the old price) until a quote of the new revision is accepted.
-- The flag marks such an item: it takes quotes and a quote's acceptance while its order
-- is accepted, in production or ready. Accepting a quote clears it.
ALTER TABLE order_items
    ADD COLUMN IF NOT EXISTS revised_after_acceptance boolean NOT NULL DEFAULT false;
