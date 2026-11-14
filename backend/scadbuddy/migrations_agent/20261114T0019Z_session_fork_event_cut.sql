-- A fork is copy-on-write for its event log and its images too (#4212): the child reads its
-- parent's conversation events through the cut (sessions/eventLog.ts `EventLog.read`) and its
-- parent's images by name (sessions/blobs.ts `SessionBlobs.get`), instead of copies made at the
-- fork. `fork_event_cut` is the seq, in the parent's log as the parent reads it, of the last
-- event the child takes. The child's own `session.started` is its seq 1 (a parent's seq 1 is its
-- own `session.started`, which a fork never takes), and its own later events come after the cut,
-- so a seq names the same event in the parent and the child up to the cut.
-- NULL on a fork made before this change: its events and images are its own copies.
-- forgetSubject copies the child's range in (sessions/store.ts `copyForkRanges`) and clears it
-- with the link.
ALTER TABLE ai_sessions
  ADD COLUMN fork_event_cut bigint CHECK (fork_event_cut >= 0),
  ADD CONSTRAINT ai_sessions_fork_event_link CHECK (fork_event_cut IS NULL OR fork_parent_id IS NOT NULL);
