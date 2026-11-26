-- The events an own retention applies to (pg_events.prune_log): its first DELETE names
-- this predicate, so it reads this index and not every row of the log. The build locks
-- writes to events (no CONCURRENTLY: a migration runs in a transaction) for a moment.
CREATE INDEX events_own_retention ON events (logged_at)
    WHERE jsonb_typeof(payload->'retention_seconds') = 'number';
