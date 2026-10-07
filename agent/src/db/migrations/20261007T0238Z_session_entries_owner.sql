-- Durable sessions (#1056, final fix wave): the ai_sessions id whose segment wrote each
-- transcript line, recorded by agent-durable's store (store.py) from its activity's
-- workflow id. forget-subject deletes a durable session's transcripts by it too: a
-- segment that failed or crashed never recorded its Claude session id in
-- ai_durable_segments, which was the only way forget found them. NULL for classic
-- sessions' lines and for lines written before this column.
ALTER TABLE ai_session_entries ADD COLUMN owner_session_id uuid;
CREATE INDEX ai_session_entries_owner ON ai_session_entries (owner_session_id) WHERE owner_session_id IS NOT NULL;
