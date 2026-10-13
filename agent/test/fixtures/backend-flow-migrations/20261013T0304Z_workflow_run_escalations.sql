-- Escalation of a flow's calls outside its grant (#2482, spec
-- 2026-10-11-flow-grants-and-git-design.md §4.6): every hop action on an entry, copied
-- from the entry into its decision when it resolves, so a flow-own entry's history reads
-- like a child's (`ai_input_responses.hops`).
ALTER TABLE workflow_run_decisions ADD COLUMN hops jsonb NOT NULL DEFAULT '[]';

-- A parent's narrowed input that the grant did not admit (§4.2): it goes up with the
-- entry as a proposal, which the person may approve as it is. Kept here, never in
-- `workflow_runs.waiting_on`, so no read of a run returns it raw. Written once per call;
-- `input_hash` is what the person's approval must name, so it approves what it was
-- shown and nothing else.
CREATE TABLE workflow_run_proposals (
    request_id  text PRIMARY KEY,
    run_id      text NOT NULL REFERENCES workflow_runs (id) ON DELETE CASCADE,
    call_id     text NOT NULL,
    args        jsonb NOT NULL,
    input_hash  text NOT NULL,
    proposed_by text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX workflow_run_proposals_run ON workflow_run_proposals (run_id);
