-- Deposits, waivers and auto tax (#5281, storefront spec §8, §9).
--
-- `deposit_minor` is what the order asks before production, in its currency. Each
-- waiver is the shop's reason to go on without the money: "Proceed without deposit"
-- and "Ship without full payment".
ALTER TABLE orders
  ADD COLUMN deposit_minor bigint CHECK (deposit_minor > 0),
  ADD COLUMN deposit_waived_reason text
    CHECK (char_length(deposit_waived_reason) BETWEEN 1 AND 500),
  ADD COLUMN balance_waived_reason text
    CHECK (char_length(balance_waived_reason) BETWEEN 1 AND 500);

-- An `auto` charge is the tax ScadBuddy computes from `orders_tax_percent`; a typed
-- charge is never `auto`. A shipping charge a shipping option wrote names that option.
ALTER TABLE order_charges
  ADD COLUMN auto boolean NOT NULL DEFAULT false,
  ADD COLUMN shipping_option_id text,
  ADD CONSTRAINT order_charges_auto_is_tax CHECK (NOT auto OR kind = 'tax'),
  ADD CONSTRAINT order_charges_option_is_shipping
    CHECK (shipping_option_id IS NULL OR kind = 'shipping');

-- At most one auto tax row per order.
CREATE UNIQUE INDEX order_charges_auto_tax ON order_charges (order_id) WHERE auto;
