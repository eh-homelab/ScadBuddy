-- Every child chat a Workflow's run starts (src/background/, #3395): an attempt of
-- `wf_run` whose worker died never ran its agents' cleanup, so the next attempt ends
-- each child here that no journal row (ai_workflow_calls.child_id) names before its
-- script runs. A row goes with its task.
CREATE TABLE ai_workflow_children (
  task_id     text NOT NULL REFERENCES ai_background_tasks (id) ON DELETE CASCADE,
  child_id    uuid NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (task_id, child_id)
);
