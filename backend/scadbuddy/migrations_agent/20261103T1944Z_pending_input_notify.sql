-- #815: one NOTIFY on `scadbuddy_pending` for every statement that writes a table of calls
-- parked on a person (the durable projection `ai_pending_input`, `ai_approvals`, and
-- `ai_questions` with its attention requests), whoever writes it (this service or
-- agent-durable). The payload is empty: it says only that the pending-input read may have
-- changed, so the badge's stream (routes/pendingInput.ts `/pending-input/changes`) re-reads.
-- Postgres sends a NOTIFY only at commit and folds identical ones in a transaction into one
-- (https://www.postgresql.org/docs/current/sql-notify.html).
CREATE FUNCTION ai_pending_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_notify('scadbuddy_pending', '');
  RETURN NULL;
END
$$;

CREATE TRIGGER ai_pending_input_changed AFTER INSERT OR UPDATE OR DELETE ON ai_pending_input
  FOR EACH STATEMENT EXECUTE FUNCTION ai_pending_changed();
CREATE TRIGGER ai_approvals_changed AFTER INSERT OR UPDATE OR DELETE ON ai_approvals
  FOR EACH STATEMENT EXECUTE FUNCTION ai_pending_changed();
CREATE TRIGGER ai_questions_changed AFTER INSERT OR UPDATE OR DELETE ON ai_questions
  FOR EACH STATEMENT EXECUTE FUNCTION ai_pending_changed();
