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
// The next entry is expected to be #251's `ai_mcp_tokens`.

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
