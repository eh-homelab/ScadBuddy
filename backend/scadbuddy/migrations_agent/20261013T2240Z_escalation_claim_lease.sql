-- When a parent's deny claimed its entry (#2861, review #3018). The claim holds the hop
-- at `hop_until = 'infinity'` while the deny's Update is sent; a process that dies before
-- putting the deadline back would otherwise leave the entry at hop `parent` for good.
-- The sweep (`moveDue`) hands a claim older than its lease to the person.
ALTER TABLE ai_pending_input ADD COLUMN hop_claimed_at timestamptz;
