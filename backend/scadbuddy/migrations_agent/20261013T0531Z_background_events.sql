-- A Monitor's events (src/background/, temporal/backgroundTask.ts): each batch of its
-- command's stdout lines, or each WebSocket frame, as Claude Code's Monitor makes one
-- notification of them. The command's activity (the agent's on the replica that ran the
-- turn, or agent-durable's for a durable session) writes them; the task's workflow sends
-- the undelivered ones to the session as its next message once it is idle, and marks
-- them delivered. They go with their task.
CREATE TABLE ai_background_events (
  task_id       text NOT NULL REFERENCES ai_background_tasks (id) ON DELETE CASCADE,
  seq           bigserial,
  text          text NOT NULL CHECK (length(text) <= 16384),
  created_at    timestamptz NOT NULL DEFAULT now(),
  delivered_at  timestamptz,
  PRIMARY KEY (task_id, seq)
);

CREATE INDEX ai_background_events_pending ON ai_background_events (task_id, seq) WHERE delivered_at IS NULL;
