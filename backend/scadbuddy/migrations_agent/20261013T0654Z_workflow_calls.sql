-- A Workflow's journal (src/background/, #2393): each agent() call's result, by its
-- key (sha256 of its prompt and options) and its occurrence among the run's calls with
-- that key. A retried run, or a resume (resumeFromRunId), answers a call from here
-- instead of running its agent again; `child_id` is the chat the agent ran in.
CREATE TABLE ai_workflow_calls (
  task_id     text NOT NULL REFERENCES ai_background_tasks (id) ON DELETE CASCADE,
  call_key    text NOT NULL CHECK (call_key ~ '^[0-9a-f]{64}$'),
  occurrence  integer NOT NULL CHECK (occurrence >= 0),
  result      jsonb NOT NULL,
  ok          boolean NOT NULL,
  child_id    uuid,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (task_id, call_key, occurrence)
);
