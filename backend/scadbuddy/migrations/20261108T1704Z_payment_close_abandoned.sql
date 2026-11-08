-- #5700 follow-up: the sweep stopped closing a page whose provider is no longer
-- connected; the order page tells the shop to close it at the provider.
ALTER TABLE payment_requests ADD COLUMN close_abandoned_at timestamptz;
