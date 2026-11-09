-- #4959: a request that an approval moved to `pending`, whose capture outcome is
-- unknown, is read again by the housekeeping sweep (`Payments.settle_captures`), with a
-- backoff per request. `capture_pending_at` is set when an approval moves the request to
-- `pending` (a bank debit's pending never sets it) and cleared when the request leaves
-- `pending`.
ALTER TABLE payment_requests
  ADD COLUMN capture_pending_at timestamptz,
  ADD COLUMN capture_tried_at timestamptz,
  ADD COLUMN capture_attempts integer NOT NULL DEFAULT 0;
CREATE INDEX payment_requests_capture_pending ON payment_requests (capture_pending_at)
  WHERE capture_pending_at IS NOT NULL;
