
      -- #258: approvals of outward tool calls (spec §8.2). See src/approvals/.
      -- One row per outward call that waited for a human. Pending while
      -- decision IS NULL; a pending row outlives a restart, and is decided,
      -- expired or cancelled later (src/approvals/service.ts).
      CREATE TABLE ai_approvals (
        id                 uuid PRIMARY KEY,
        -- NULL for an approval outside a session (#251's MCP prepare/confirm).
        session_id         uuid REFERENCES ai_sessions (id) ON DELETE CASCADE,
        -- The turn that parked on it; NULL when none did.
        turn_id            uuid,
        -- The tool_use block's id (the panel's tool.call id).
        tool_use_id        text NOT NULL,
        tool               text NOT NULL,
        -- The input as the event log shows it: scrubbed (sessions/sdkEvents.ts
        -- scrubForLog). The full input is never stored here.
        input_summary      text NOT NULL,
        -- HMAC-SHA256 (server-side key) of the tool name and the canonical
        -- JSON of the full input; a decision applies to this exact input only.
        input_hash         text NOT NULL,
        tier               text NOT NULL CHECK (tier IN ('read', 'write', 'outward')),
        requested_by_kind  text NOT NULL,
        requested_by_id    text NOT NULL,
        requested_by_label text NOT NULL,
        created_at         timestamptz NOT NULL DEFAULT now(),
        expires_at         timestamptz NOT NULL,
        decision           text CHECK (decision IN ('approved', 'denied', 'expired', 'cancelled')),
        decided_by_kind    text,
        decided_by_id      text,
        decided_by_label   text,
        decided_at         timestamptz,
        -- Why it was cancelled, expired or voided, for the audit trail.
        reason             text,
        -- Approved: usable until then (the expiry window, from the decision).
        usable_until       timestamptz,
        -- Approved after its turn was gone: the resumed turn that may use it.
        resume_turn_id     uuid,
        -- Set once the approved call ran: an approval is used at most once.
        consumed_at        timestamptz,
        -- Approved but voided unused (interrupt, handoff, a new turn, its
        -- turn ended, a failed resume).
        revoked_at         timestamptz,
        CHECK ((decision IS NULL) = (decided_at IS NULL)),
        CHECK ((decision = 'approved') = (usable_until IS NOT NULL)),
        CHECK (consumed_at IS NULL OR decision = 'approved'),
        CHECK (revoked_at IS NULL OR (decision = 'approved' AND consumed_at IS NULL))
      );
      CREATE INDEX ai_approvals_session ON ai_approvals (session_id, created_at);
      CREATE INDEX ai_approvals_pending ON ai_approvals (expires_at) WHERE decision IS NULL;
    