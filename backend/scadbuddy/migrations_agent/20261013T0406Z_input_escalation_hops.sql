-- Escalation of a flow run's child calls (#2482, spec 2026-10-11 §4.4, §4.6). A durable
-- child's call in a flow turn is in its run's queue (`flow_run_id`). Outside the run's
-- grant it waits first at hop `parent`, the session that started the run, until
-- `hop_until`, then at hop `person`. Who the parent is (`parent_session`, and the
-- principal that owned it when the run started, `parent_principal`) comes from the
-- backend's record of the run (`flow_grant_check`'s verdict), never from a caller: the
-- escalation tool authorises the parent's answer against these columns. `hops` is every
-- step of the way up ({hop, action, actor, at, reason?}), copied into the response when
-- the entry resolves. `delivered_at` is when the starter session was sent the entry.
ALTER TABLE ai_pending_input
  ADD COLUMN flow_run_id      text,
  ADD COLUMN hop              text CHECK (hop IN ('parent', 'person')),
  ADD COLUMN hop_until        timestamptz,
  ADD COLUMN parent_session   uuid,
  ADD COLUMN parent_principal text,
  ADD COLUMN outside_reason   text,
  ADD COLUMN hops             jsonb NOT NULL DEFAULT '[]',
  ADD COLUMN delivered_at     timestamptz,
  ADD CHECK (hop IS DISTINCT FROM 'parent' OR (parent_session IS NOT NULL AND parent_principal IS NOT NULL AND hop_until IS NOT NULL));
CREATE INDEX ai_pending_input_flow_run ON ai_pending_input (flow_run_id) WHERE flow_run_id IS NOT NULL;
CREATE INDEX ai_pending_input_parent ON ai_pending_input (parent_session) WHERE hop = 'parent';

ALTER TABLE ai_input_responses ADD COLUMN hops jsonb NOT NULL DEFAULT '[]';
