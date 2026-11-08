-- #5681: a refund_failed event's row names the refund event it reverses, so a refund
-- told after its own failure (Stripe does not keep event order) is found and writes
-- nothing.
ALTER TABLE payment_events ADD COLUMN reverses text;
CREATE INDEX payment_events_reverses ON payment_events (provider, reverses)
  WHERE reverses IS NOT NULL;
