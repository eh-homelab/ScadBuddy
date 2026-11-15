-- #4212: a fork's transcript load reads each ancestor's main-transcript rows in load order
-- (`inherited DESC, id`) only up to the least cut below it (src/sessions/store.ts
-- `chainRows`, a LATERAL subquery with `LIMIT c.bound`). An index in that order lets the
-- limit stop the scan, so the ancestor's rows past the fork point are not read; on
-- `ai_session_entries_key (session_id, subpath, id)` alone, Postgres reads them all and
-- sorts them before the limit.
CREATE INDEX ai_session_entries_load ON ai_session_entries (session_id, subpath, inherited DESC, id);
