-- What a Reset needs of a flow run's row (plan 2026-10-09-durable-phase-6-flows.md Task
-- E2). superseded_run_ids: the executions a Reset replaced, whose projection writes still
-- in flight are ignored, so they never move the row back. waiting_done: the parked
-- entries already resolved, each with the history length it was resolved at, so a Reset
-- to before that point parks them again. reset_request_ids: beside each superseded run
-- id, the id of the Reset operation that replaced it, so a retry of that operation is
-- told apart from a different Reset that got there first.
ALTER TABLE workflow_runs
    ADD COLUMN superseded_run_ids text[] NOT NULL DEFAULT '{}',
    ADD COLUMN reset_request_ids text[] NOT NULL DEFAULT '{}',
    ADD COLUMN waiting_done jsonb NOT NULL DEFAULT '[]';
