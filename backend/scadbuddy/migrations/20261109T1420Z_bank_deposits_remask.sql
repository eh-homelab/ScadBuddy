-- The bank text stored before #5852 kept every order-number match as written, so an
-- account number written as one (`ORD-2026-000123456789`, or digits run on after it,
-- `ORD-2026-12 3456`) could be stored unmasked (#4961). The unmasked text is never kept,
-- so the canonical number cannot be rebuilt. First each order number, with its digits run
-- on across single spaces or hyphens, becomes `ORD-•••`; then every run of four or more
-- digits left becomes `•••`. A real order's number is masked too; that loses only a hint
-- on an old row.
UPDATE bank_deposits
SET description = regexp_replace(
  regexp_replace(description, '\mORD-[0-9](?:[ -]?[0-9])*', 'ORD-•••', 'gi'),
  '[0-9]{4,}', '•••', 'g'
)
WHERE description ~ '[0-9]{4,}' OR description ~* '\mORD-[0-9]';
