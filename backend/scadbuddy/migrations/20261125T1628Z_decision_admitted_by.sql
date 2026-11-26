-- #7471: who besides a person answered a flow's question. `responder` names the answerer
-- (`browser`, `session:<id>`, `token:<id>`); `admitted_by` what let an agent answer: `setting`
-- (`flow_answerers`), or a run grant's id once grants may. NULL for a person's answer, a
-- decision on an approval, and rows from before it.
ALTER TABLE workflow_run_decisions ADD COLUMN admitted_by text;
