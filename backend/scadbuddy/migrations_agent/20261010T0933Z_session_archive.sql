-- #1885: archiving a chat (sessions/edits.ts, PATCH /api/v1/ai/sessions/:id
-- {archived}). When its owner archived it; NULL while it is not archived. An
-- archived session is left out of the panel's `sessions.snapshot` and of every
-- list unless asked for (sessions/manager.ts `listQuery`), and is read-only: a
-- send or a handoff is refused with `archived`, a fork is not. Unarchiving sets
-- it back to NULL. Shared by every viewer, and kept only here.
ALTER TABLE ai_sessions ADD COLUMN archived_at timestamptz;
