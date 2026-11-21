-- #6836: when the last pass started, by the clock of the worker that ran it. "Sync now"
-- (`POST /filament/sync/run`) answers the API's `requested_at`, and the page waits for a pass
-- that started at or after it, never comparing the browser's clock with the server's.
-- Null until a pass records it.
ALTER TABLE filament_bb_sync_state ADD COLUMN last_pass_started_at timestamptz;
