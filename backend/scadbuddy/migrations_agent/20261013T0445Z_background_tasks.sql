-- The background built-ins of a session (CronCreate, ScheduleWakeup, Monitor,
-- Workflow; src/background/): Claude Code runs them inside its own process, which a
-- turn ends, so the agent keeps them instead, for classic and durable sessions alike.
-- Each active row has a Temporal workflow `bgtask-<id>` on `agent-tools`
-- (temporal/backgroundTask.ts) that fires it: a prompt sent as the session's next
-- message when the session is idle. The workflow writes `next_at`, `fired`,
-- `last_fired_at` and the end; the panel lists the rows and cancels through the
-- workflow (routes/backgroundTasks.ts).
--
-- `spec` is the tool's input as accepted (the cron expression and prompt, the
-- wakeup's prompt). `created_by` is who owned the turn that created it: a fire runs
-- as them, and only while they still own the session.
CREATE TABLE ai_background_tasks (
  id              text PRIMARY KEY CHECK (id ~ '^[0-9a-f]{8}$'),
  session_id      uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
  kind            text NOT NULL CHECK (kind IN ('cron', 'wakeup', 'monitor', 'workflow')),
  description     text NOT NULL CHECK (length(description) BETWEEN 1 AND 500),
  spec            jsonb NOT NULL,
  created_by      jsonb NOT NULL,
  state           text NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'done', 'cancelled', 'failed')),
  next_at         timestamptz,
  fired           integer NOT NULL DEFAULT 0,
  last_fired_at   timestamptz,
  expires_at      timestamptz,
  detail          text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ai_background_tasks_session ON ai_background_tasks (session_id, created_at);
