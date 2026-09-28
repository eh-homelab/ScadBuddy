-- #546: the legacy render queue is gone, and with it the lease its workers renewed.
-- A render's liveness is Temporal's now (activity heartbeats and timeouts).
DROP INDEX IF EXISTS render_jobs_running;
ALTER TABLE render_jobs DROP COLUMN IF EXISTS heartbeat_at;
