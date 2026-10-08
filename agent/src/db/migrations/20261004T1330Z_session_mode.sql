-- A session's mode (spec 2026-10-01 §6.1): `classic` runs in the agent service's
-- harness, `durable` as a DurableSession workflow (phase 5). Set at insert, never
-- updated. Added with the agent-tools worker (#1055) because a tool activity runs
-- only for a durable session: its workflow made the approval the activity skips.
ALTER TABLE ai_sessions
  ADD COLUMN mode text NOT NULL DEFAULT 'classic' CHECK (mode IN ('classic', 'durable'));
