-- #6386: a session's queue is read from its last `session.queue` event
-- (agent/src/sessions/durable.ts `queue`), on every read of the session. This partial index
-- holds only those events, newest first per session, so the read does not parse every event
-- the session has.
CREATE INDEX ai_session_events_queue ON ai_session_events (session_id, seq DESC)
  WHERE (event::jsonb ->> 'type') = 'session.queue';
