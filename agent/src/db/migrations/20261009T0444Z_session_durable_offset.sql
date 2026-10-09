-- A durable session's live output (plan 5c Ruling 6): the next offset of the
-- DurableSession workflow's stream that follow_session has not yet written to
-- ai_session_events. Moved in the transaction that appends the events, so a
-- retried or doubled subscriber writes each event once.
ALTER TABLE ai_sessions ADD COLUMN durable_offset bigint NOT NULL DEFAULT 0;
