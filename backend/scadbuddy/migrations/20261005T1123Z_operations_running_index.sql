-- The lost-operation reconciler scans `status = 'running'` rows every few minutes on each
-- replica; with no retention set the table keeps every operation, so index only the few
-- running rows (review #1063).
CREATE INDEX operations_running ON operations (created_at) WHERE status = 'running';
