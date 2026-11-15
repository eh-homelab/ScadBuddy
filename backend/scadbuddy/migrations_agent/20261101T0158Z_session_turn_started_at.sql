-- #5050: when the session's running turn was claimed (sessions/durable.ts `send`), which the
-- done summary takes as the turn's start. Each placement of a turn reads the session again,
-- and `updated_at` moves with every event and status change, so it cannot stand in.
-- NULL: no turn claimed since this file; CLAIMED_AT then falls back to `updated_at`.
ALTER TABLE ai_sessions ADD COLUMN turn_started_at timestamptz;
