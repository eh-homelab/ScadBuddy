-- #4959 (#5776 review): a request that an approval moved to `pending` before
-- `capture_pending_at` existed was never marked, so the capture sweep never read it.
-- Mark each request still `pending` whose latest `payment_request.pending` audit row was
-- an approval; the sweep then reads it from its first wait. A bank debit's pending row
-- names no approval and stays unmarked.
UPDATE payment_requests r SET capture_pending_at = now()
WHERE r.status = 'pending' AND r.capture_pending_at IS NULL
  AND (
    SELECT (e.detail->>'approved')::boolean FROM order_events e
    WHERE e.order_id = r.order_id AND e.kind = 'payment_request.pending'
      AND e.detail->>'request_id' = r.id::text
    ORDER BY e.id DESC LIMIT 1
  ) IS TRUE;
