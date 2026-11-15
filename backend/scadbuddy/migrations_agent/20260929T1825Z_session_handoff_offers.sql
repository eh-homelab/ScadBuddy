-- #300 (PR #715 review): handing a session to another MCP principal is an
-- offer that principal accepts, not a transfer (spec §6 "Handoff": ownership
-- moves "explicitly"). The pending offer lives on the session row: whom it is
-- offered to (a principal, as the Owner {kind, id, label}) and until when.
-- Only that principal may accept it; the owner may withdraw it, the target may
-- decline it, and any change of owner clears it. See
-- src/sessions/manager.ts `handoff` and src/tools/sessions.ts.
--
-- Nothing is offered for every existing session.
ALTER TABLE ai_sessions
  ADD COLUMN pending_owner_kind  text,
  ADD COLUMN pending_owner_id    text,
  ADD COLUMN pending_owner_label text,
  ADD COLUMN pending_owner_until timestamptz,
  ADD CONSTRAINT ai_sessions_pending_owner CHECK (
    (pending_owner_kind IS NULL) = (pending_owner_id IS NULL)
    AND (pending_owner_kind IS NULL) = (pending_owner_label IS NULL)
    AND (pending_owner_kind IS NULL) = (pending_owner_until IS NULL)
  );
-- list() also returns the sessions offered to the caller (manager.ts
-- listQuery); a third index lets Postgres BitmapOr it with the owner and
-- creator ones. Partial: almost no session has an offer.
CREATE INDEX ai_sessions_pending_owner ON ai_sessions (pending_owner_kind, pending_owner_id, updated_at DESC)
  WHERE pending_owner_kind IS NOT NULL;
