-- A fork is copy-on-write for the transcript (plan 2026-10-13 Step 6.5, decision Q8, #4212):
-- the child reads its parent's main transcript through the cut, then its own rows
-- (sessions/store.ts `PostgresSessionStore.load`), instead of a copy made at the fork.
-- `fork_parent_id` is the session it reads through; `fork_cut` is how many entries of that
-- parent's main transcript, as the parent loads it, the child takes. A position rather than a
-- row id: once a forgotten parent's range is copied into its child (sessions/forget.ts), the
-- copies have new ids, and a grandchild's cut must still name the same point.
-- RESTRICT: a parent's row goes only after forgetSubject has copied each child's range into it
-- and cleared the child's link.
ALTER TABLE ai_sessions
  ADD COLUMN fork_parent_id uuid REFERENCES ai_sessions (id) ON DELETE RESTRICT,
  ADD COLUMN fork_cut integer CHECK (fork_cut >= 0),
  ADD CONSTRAINT ai_sessions_fork_link CHECK ((fork_parent_id IS NULL) = (fork_cut IS NULL));
CREATE INDEX ai_sessions_fork_parent ON ai_sessions (fork_parent_id) WHERE fork_parent_id IS NOT NULL;

-- A row copied into a child from its forgotten parent's range: it is read before the child's
-- own rows, whatever its id, and it is not one of the child's own turns (turnHost.ts's mark,
-- warm.ts's tail and last `cost-state`).
ALTER TABLE ai_session_entries ADD COLUMN inherited boolean NOT NULL DEFAULT false;
