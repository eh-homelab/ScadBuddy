-- #258 (AI: safety — confirmations, audit log, prompt-injection hardening): the
-- audit log of AI actions (spec §8.3, §8.6, §9). See src/audit/log.ts.
--
-- Append-only. One row per tool call made by the harness (a session turn) or
-- over /mcp, per approval decision, and per credential, plugin, settings or
-- MCP-token write. Nothing secret is stored: `input_summary` is scrubbed by
-- sessions/sdkEvents.ts scrubForLog, and `input_hash` is the same keyed
-- HMAC-SHA256 the approvals use (approvals/service.ts inputHash), so a row can
-- be matched to its approval without the input being recoverable from it.
CREATE TABLE ai_audit (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at               timestamptz NOT NULL DEFAULT now(),
  -- What kind of action: a tool call, an approval decision, or a write.
  kind             text NOT NULL CHECK (kind IN ('tool_call', 'approval', 'credential', 'plugin', 'settings', 'token')),
  -- The tool name for a tool call; otherwise the verb (e.g. 'approved', 'mint', 'PUT').
  action           text NOT NULL,
  -- Where it came from: a session turn, /mcp, or an HTTP route of the UI.
  surface          text NOT NULL CHECK (surface IN ('harness', 'mcp', 'http', 'system')),
  -- Who (spec §8.1), as sessions/protocol.ts Owner {kind, id, label}.
  principal_kind   text NOT NULL,
  principal_id     text NOT NULL,
  principal_label  text NOT NULL,
  -- Spec §8.3: `disabled` mode records the client IP; recorded whenever known.
  client_ip        text,
  session_id       uuid,
  turn_id          uuid,
  tool_use_id      text,
  tier             text CHECK (tier IN ('read', 'write', 'outward')),
  input_hash       text,
  input_summary    text,
  approval_id      uuid,
  -- ok: ran and succeeded; error: ran (or tried to) and failed; refused: not
  -- run by policy (tier, no approval surface, pending, expired, cancelled);
  -- denied: a human said no.
  outcome          text NOT NULL CHECK (outcome IN ('ok', 'error', 'refused', 'denied')),
  -- A short, scrubbed reason or target (never a secret).
  detail           text,
  started_at       timestamptz,
  finished_at      timestamptz,
  duration_ms      integer CHECK (duration_ms IS NULL OR duration_ms >= 0)
);

CREATE INDEX ai_audit_at ON ai_audit (at DESC, id DESC);
CREATE INDEX ai_audit_kind_at ON ai_audit (kind, at DESC, id DESC);
CREATE INDEX ai_audit_session ON ai_audit (session_id, at DESC) WHERE session_id IS NOT NULL;
CREATE INDEX ai_audit_action ON ai_audit (action, at DESC);

-- Append-only, enforced in the database: rows are never updated, and are
-- deleted only by the retention sweep (src/audit/log.ts prune), which sets
-- `scadbuddy.audit_prune` for its own transaction. This stops the service's
-- own code (or a stray statement) from rewriting history; it is no defence
-- against a database superuser, who can drop the trigger.
CREATE FUNCTION ai_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' AND current_setting('scadbuddy.audit_prune', true) = 'on' THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'ai_audit is append-only (% refused)', TG_OP;
END
$$;

CREATE TRIGGER ai_audit_no_update BEFORE UPDATE ON ai_audit
  FOR EACH ROW EXECUTE FUNCTION ai_audit_append_only();
CREATE TRIGGER ai_audit_no_delete BEFORE DELETE ON ai_audit
  FOR EACH ROW EXECUTE FUNCTION ai_audit_append_only();
CREATE TRIGGER ai_audit_no_truncate BEFORE TRUNCATE ON ai_audit
  FOR EACH STATEMENT EXECUTE FUNCTION ai_audit_append_only();
