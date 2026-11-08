-- A flow run's links (#4252, orders spec §7.2, §7.4, Ruling 9): the run that started it,
-- when a run's `tool("start_flow_run", …)` did (a linked run, not a child workflow), and
-- the order it is for. Written by the API when it starts the run; `workflow_runs` stays
-- the run's workflow's own.
CREATE TABLE workflow_run_links (
    run_id        text PRIMARY KEY,
    parent_run_id text,
    order_id      text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (parent_run_id IS NOT NULL OR order_id IS NOT NULL)
);
CREATE INDEX workflow_run_links_parent ON workflow_run_links (parent_run_id)
    WHERE parent_run_id IS NOT NULL;
CREATE INDEX workflow_run_links_order ON workflow_run_links (order_id) WHERE order_id IS NOT NULL;

-- A child run's grant, seeded from its parent run's (§7.2): it spends only while that one
-- is in force, and a revocation of the parent's revokes it too. A deleted parent's run
-- revokes it first (`FlowStore._delete_run`); the link goes, the child's row and its uses
-- (the audit) stay.
ALTER TABLE flow_grants ADD COLUMN parent_grant_id text REFERENCES flow_grants (id) ON DELETE SET NULL;
CREATE INDEX flow_grants_parent ON flow_grants (parent_grant_id) WHERE parent_grant_id IS NOT NULL;
