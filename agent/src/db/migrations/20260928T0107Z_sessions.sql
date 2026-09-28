
      -- Session metadata (spec §6). The id IS the Agent SDK session id: the
      -- first turn passes it as the SDK's `sessionId` option.
      CREATE TABLE ai_sessions (
        id               uuid PRIMARY KEY,
        origin           text NOT NULL CHECK (origin IN ('chat', 'mcp', 'analyzer', 'hook')),
        -- Owner principal (spec §8.1), as the panel protocol's Owner {kind, id, label}.
        owner_kind       text NOT NULL,
        owner_id         text NOT NULL,
        owner_label      text NOT NULL,
        -- Who started it; a principal keeps seeing a session it handed off.
        creator_kind     text NOT NULL,
        creator_id       text NOT NULL,
        status           text NOT NULL CHECK (status IN
                           ('running', 'waiting_input', 'waiting_approval', 'idle', 'done', 'failed')),
        title            text NOT NULL DEFAULT '',
        tags             text[] NOT NULL DEFAULT '{}',
        -- Scope (spec §6: model slug, output, job), free-form.
        scope            jsonb NOT NULL DEFAULT '{}',
        parent_id        uuid REFERENCES ai_sessions (id) ON DELETE SET NULL,
        -- Per-session limits, fixed at start from ai_settings.
        max_turns        integer NOT NULL CHECK (max_turns > 0),
        budget_usd       double precision NOT NULL CHECK (budget_usd > 0),
        cost_usd         double precision NOT NULL DEFAULT 0,
        turns            integer NOT NULL DEFAULT 0,
        -- The turn claim: one active turn per session across replicas. A turn
        -- holds it while lease_until is in the future and renews it; a replica
        -- that dies mid-turn loses it when the lease runs out.
        turn_id          uuid,
        lease_until      timestamptz,
        interrupt_requested boolean NOT NULL DEFAULT false,
        -- Last ai_session_events.seq handed out for this session.
        event_seq        bigint NOT NULL DEFAULT 0,
        created_at       timestamptz NOT NULL DEFAULT now(),
        updated_at       timestamptz NOT NULL DEFAULT now(),
        CHECK ((turn_id IS NULL) = (lease_until IS NULL))
      );
      -- list() for a non-browser principal filters on owner OR creator
      -- (src/sessions/manager.ts listQuery); one index per side lets Postgres
      -- BitmapOr them instead of scanning the table. The browser's unfiltered
      -- list reads ai_sessions_updated.
      CREATE INDEX ai_sessions_owner ON ai_sessions (owner_kind, owner_id, updated_at DESC);
      CREATE INDEX ai_sessions_creator ON ai_sessions (creator_kind, creator_id, updated_at DESC);
      CREATE INDEX ai_sessions_updated ON ai_sessions (updated_at DESC);

      -- The Agent SDK SessionStore mirror (src/sessions/store.ts): one row per
      -- transcript line, stored as JSON text so it round-trips exactly (jsonb
      -- rejects \u0000). Not keyed to ai_sessions: the SDK writes a fork's
      -- lines before ScadBuddy records the fork.
      CREATE TABLE ai_session_entries (
        id          bigserial PRIMARY KEY,
        project_key text NOT NULL,
        session_id  text NOT NULL,
        -- '' is the main transcript; the SDK's subpath otherwise.
        subpath     text NOT NULL DEFAULT '',
        uuid        text,
        entry       text NOT NULL,
        created_at  timestamptz NOT NULL DEFAULT now()
      );
      CREATE INDEX ai_session_entries_key ON ai_session_entries (session_id, subpath, id);
      CREATE INDEX ai_session_entries_project ON ai_session_entries (project_key, session_id);
      CREATE UNIQUE INDEX ai_session_entries_uuid
        ON ai_session_entries (session_id, subpath, uuid) WHERE uuid IS NOT NULL;

      -- The panel-protocol events of each session, in order, for attach replay
      -- and for watchers on other replicas (src/sessions/eventLog.ts).
      CREATE TABLE ai_session_events (
        session_id uuid NOT NULL REFERENCES ai_sessions (id) ON DELETE CASCADE,
        seq        bigint NOT NULL,
        event      text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (session_id, seq)
      );
    