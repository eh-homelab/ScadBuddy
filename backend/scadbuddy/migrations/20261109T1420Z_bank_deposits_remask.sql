-- The bank text stored before #5852 kept every order-number match as written, so an
-- account number written as one (`ORD-2026-000123456789`, or digits run on after it,
-- `ORD-2026-12 3456`) could be stored unmasked (#4961). The unmasked text is never kept,
-- so the canonical number cannot be rebuilt. First each order number, with its digits run
-- on across single spaces or hyphens, becomes `ORD-•••`; then every run of four or more
-- digits left becomes `•••`, a run going on across single spaces or hyphens as `mask`
-- counts it (`12 34 56 78`). A real order's number is masked too; that loses only a hint
-- on an old row.
--
-- The text has two copies: `bank_deposits.description`, and the notes of the payment a
-- confirmed deposit recorded (`deposits.record_deposit`: method `bank_transfer`, reference
-- the transaction id, the row's `payment_id`). `order_events.detail` holds ids only.
UPDATE bank_deposits
SET description = regexp_replace(
  regexp_replace(description, '\mORD-[0-9](?:[ -]?[0-9])*', 'ORD-•••', 'gi'),
  '[0-9](?:[ -]?[0-9]){3,}', '•••', 'g'
)
WHERE description ~ '[0-9](?:[ -]?[0-9]){3,}' OR description ~* '\mORD-[0-9]';

UPDATE order_payments p
SET notes = regexp_replace(
  regexp_replace(p.notes, '\mORD-[0-9](?:[ -]?[0-9])*', 'ORD-•••', 'gi'),
  '[0-9](?:[ -]?[0-9]){3,}', '•••', 'g'
)
WHERE p.method = 'bank_transfer'
  AND (p.notes ~ '[0-9](?:[ -]?[0-9]){3,}' OR p.notes ~* '\mORD-[0-9]')
  AND (
    EXISTS (SELECT 1 FROM bank_deposits d WHERE d.payment_id = p.id)
    OR EXISTS (SELECT 1 FROM bank_deposits d WHERE d.transaction_id = p.reference)
  );
