-- #5700: a provider's page that a cancel or a manual payment closed here, not yet
-- closed at the provider. The housekeeping sweep tries again, with a backoff from the
-- last try, until the provider confirms; a confirm clears close_pending_at.
ALTER TABLE payment_requests
  ADD COLUMN close_pending_at timestamptz,
  ADD COLUMN close_attempts integer NOT NULL DEFAULT 0,
  ADD COLUMN close_tried_at timestamptz;
CREATE INDEX payment_requests_close_pending ON payment_requests (close_pending_at)
  WHERE close_pending_at IS NOT NULL;
