-- The choices a file's refusal was of (#3932 review): a later edit is stale against
-- these, not against the last edit, so editing back to them clears it. Set with each
-- refused outcome, cleared with any other.
ALTER TABLE print_batch_items ADD COLUMN refused_request jsonb;
