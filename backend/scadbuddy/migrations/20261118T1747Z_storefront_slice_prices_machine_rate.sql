-- #5774: the machine rate's parts that priced a storefront variant (`MachineRateBreakdown`
-- as JSON), kept with its price so the checkout's `storefront` quote records them, as a
-- back-office slice quote does. Null when the book's rate priced it, and on a row written
-- before this column: its quote then records no breakdown.
ALTER TABLE storefront_slice_prices ADD COLUMN machine_rate_breakdown jsonb;
