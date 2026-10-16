-- #3013 review: the housekeeping prune deletes every slice start older than a day
-- (`DELETE ... WHERE created_at < ...`, every 300 s). Both earlier indexes on
-- created_at are partial, so that delete had none to use and scanned the table.
CREATE INDEX IF NOT EXISTS print_slice_starts_created_at
    ON print_slice_starts (created_at);
