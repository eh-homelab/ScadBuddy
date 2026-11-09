-- Plaid says the bank needs the shop to sign in again (#4961: an `ITEM_LOGIN_REQUIRED`
-- webhook or sync error). Settings shows "Reconnect" while it is set; Link's update mode,
-- a `LOGIN_REPAIRED` webhook or a sync that reads Plaid clears it.
ALTER TABLE bank_links ADD COLUMN login_required_at timestamptz;
