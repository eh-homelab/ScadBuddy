import type { Sql } from 'postgres'

// Schema for the `ai_*` tables the agent service owns (design spec §9:
// "All AI state lives in the #241 database, in `ai_*` tables owned and
// migrated by the agent service").
//
// Same approach as the backend's render queue (backend/scadbuddy/render/pg_store.py,
// #241): an append-only list, applied in order inside one transaction under a
// transaction-scoped advisory lock so two starting pods cannot race, each
// version recorded once. The ledger is the agent's own `ai_migrations`, not the
// backend's `scadbuddy_migrations`: the two services migrate independently, and
// numbering them in one table would make each depend on the other's history.
//
// RULES FOR ADDING ONE
//   - Append a new entry at the end. Never edit, reorder or remove an entry that
//     has merged: a database records versions by POSITION (1-based), so a changed
//     entry is silently skipped wherever the old one already ran.
//   - One story, one entry, named in its comment. Prefix every table `ai_`.
//   - Plain SQL, no parameters (it runs through the simple query protocol, so
//     several statements per entry are fine).
//   - Secrets are never stored in the clear: use the envelope columns from
//     migration 1 (`*_sealed bytea` + `dek_sealed bytea` + `kek_id text`,
//     sealed by src/secrets.ts). Tokens that are only ever compared (MCP bearer
//     tokens, #251) are stored hashed instead, per spec §8.1.
//
// Entry 2 is #300's sessions. #251's `ai_mcp_tokens` (PR #368) goes after
// whatever is last on main when it merges (entry 3 if nothing else lands first).

/** `pg_advisory_xact_lock` key ("SCADAGNT" in ASCII); distinct from the backend's "SCADBDDY". */
export const MIGRATION_LOCK = 0x5343_4144_4147_4e54n

export type Migration = { story: string; sql: string }

export const MIGRATIONS: readonly Migration[] = [
  {
    // 1 — #255: the Claude credential and generic AI settings.
    story: '#255',
    sql: `
      -- One row per credential; the service uses id 'default' (src/credentials.ts).
      -- kind 'anthropic_api_key' reaches the SDK as ANTHROPIC_API_KEY; 'gateway'
      -- as ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN (src/harness/run.ts).
      CREATE TABLE ai_credentials (
        id            text PRIMARY KEY,
        kind          text NOT NULL CHECK (kind IN ('anthropic_api_key', 'gateway')),
        base_url      text,
        secret_sealed bytea NOT NULL,
        dek_sealed    bytea NOT NULL,
        kek_id        text NOT NULL,
        last4         text NOT NULL,
        created_at    timestamptz NOT NULL DEFAULT now(),
        updated_at    timestamptz NOT NULL DEFAULT now(),
        CHECK ((kind = 'gateway') = (base_url IS NOT NULL))
      );

      -- Non-secret AI settings (model, budget caps, ...), one JSON value per key.
      -- Never put a secret here: values are returned to Settings as they are.
      CREATE TABLE ai_settings (
        key        text PRIMARY KEY,
        value      jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
      );
    `,
  },
  {
    // 2 — #300: durable sessions (spec §6). See src/sessions/.
    story: '#300',
    sql: `
      -- Session metadata (spec §6). The id IS the Agent SDK session id: the
      -- first turn passes it as the SDK's \`sessionId\` option.
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
      CREATE INDEX ai_sessions_owner ON ai_sessions (owner_id, updated_at DESC);
      CREATE INDEX ai_sessions_updated ON ai_sessions (updated_at DESC);

      -- The Agent SDK SessionStore mirror (src/sessions/store.ts): one row per
      -- transcript line, stored as JSON text so it round-trips exactly (jsonb
      -- rejects \\u0000). Not keyed to ai_sessions: the SDK writes a fork's
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
    `,
  },
]

/** Applies the migrations this database has not seen; returns their versions. */
export async function migrate(sql: Sql, migrations: readonly Migration[] = MIGRATIONS): Promise<number[]> {
  return sql.begin(async (tx) => {
    await tx`SELECT pg_advisory_xact_lock(${MIGRATION_LOCK.toString()}::bigint)`
    await tx`
      CREATE TABLE IF NOT EXISTS ai_migrations (
        version    integer PRIMARY KEY,
        story      text NOT NULL,
        applied_at timestamptz NOT NULL DEFAULT now()
      )`
    const rows = await tx<{ version: number }[]>`SELECT version FROM ai_migrations`
    const done = new Set(rows.map((row) => row.version))
    const applied: number[] = []
    for (const [index, migration] of migrations.entries()) {
      const version = index + 1
      if (done.has(version)) continue
      await tx.unsafe(migration.sql)
      await tx`INSERT INTO ai_migrations (version, story) VALUES (${version}, ${migration.story})`
      applied.push(version)
    }
    return applied
  })
}
