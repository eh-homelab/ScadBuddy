-- #4958: a request paid by a method that settles later (a US bank debit) is `pending`
-- until the provider says it settled, and `failed` when it did not.
ALTER TABLE payment_requests DROP CONSTRAINT payment_requests_status_check;
ALTER TABLE payment_requests ADD CONSTRAINT payment_requests_status_check
  CHECK (status IN ('open', 'pending', 'paid', 'expired', 'cancelled', 'failed'));
