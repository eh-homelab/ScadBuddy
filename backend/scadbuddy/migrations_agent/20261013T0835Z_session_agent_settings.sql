-- A Workflow's agent() options (#2393, background/workflowAgents.ts), kept on the child
-- chat it runs in: the model and effort its turns run with, in place of Settings', and
-- the subagent definition (the plugin's agents/<name>.md) its turns run as. Null on
-- every other chat.
ALTER TABLE ai_sessions
  ADD COLUMN agent_model  text CHECK (agent_model ~ '^[A-Za-z0-9._:\[\]-]{1,100}$'),
  ADD COLUMN agent_effort text CHECK (agent_effort IN ('low', 'medium', 'high', 'xhigh', 'max')),
  ADD COLUMN agent_type   text CHECK (agent_type ~ '^[a-z0-9][a-z0-9-]{0,63}$');
