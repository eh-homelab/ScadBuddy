import { createHash } from 'node:crypto'
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import type { Sql, TransactionSql } from 'postgres'

// Schema for the `ai_*` tables the agent service owns (design spec §9:
// "All AI state lives in the #241 database, in `ai_*` tables owned and
// migrated by the agent service").
//
// Same approach as the backend's (backend/scadbuddy/render/pg_store.py): one
// file per migration, applied inside one transaction under a transaction-scoped
// advisory lock so two starting pods cannot race, each recorded once by its
// file id. The ledger is the agent's own `ai_migrations`, not the backend's
// `scadbuddy_migrations`: the two services migrate independently.
//
// RULES FOR ADDING ONE (#491)
//   - Add a NEW file to ./migrations/, named `<UTC timestamp>_<slug>.sql`,
//     e.g. `20260928T0612Z_mcp_tokens.sql` (`date -u +%Y%m%dT%H%MZ`). The
//     stem is its id in `ai_migrations`. Nothing else is edited, so two PRs
//     adding migrations never conflict.
//   - Files apply in timestamp order, and a file is applied whenever it is not
//     in the ledger yet: one with an OLDER timestamp that merges later (a branch
//     cut earlier) still runs, after the newer ones already applied. So a
//     migration may depend only on files already on main when it is written,
//     never on another in-flight branch.
//   - Never edit, rename or remove a file that has merged. `migrate` records
//     each file's SHA-256 and refuses to start when an applied file's bytes have
//     changed, so an edit fails loudly (even whitespace counts; .editorconfig
//     leaves this directory's whitespace alone for that reason).
//   - One story per file, named in a leading comment. Prefix every table `ai_`.
//   - Plain SQL, no parameters (it runs through the simple query protocol, so
//     several statements per file are fine).
//   - Secrets are never stored in the clear: use the envelope columns of the
//     credentials migration (`*_sealed bytea` + `dek_sealed bytea` + `kek_id
//     text`, sealed by src/secrets.ts). Tokens that are only ever compared (MCP
//     bearer tokens, #251) are stored hashed instead, per spec §8.1.
//
// The files ship next to the compiled module: `pnpm build` copies
// src/db/migrations/ to dist/db/migrations/, and MIGRATIONS is read at import,
// so an image without them fails at start instead of migrating nothing.

/** `pg_advisory_xact_lock` key ("SCADAGNT" in ASCII); distinct from the backend's "SCADBDDY". */
export const MIGRATION_LOCK = 0x5343_4144_4147_4e54n

/** One file in ./migrations/: `id` is its name without `.sql`. */
export type Migration = { id: string; sql: string }

/** `<yyyymmdd>T<hhmm>Z_<slug>`; fixed width, so sorting ids sorts by time. */
export const MIGRATION_ID = /^\d{8}T\d{4}Z_[a-z0-9_]+$/

export const MIGRATIONS_DIR = new URL('./migrations/', import.meta.url)

/**
 * The migration files of `dir`, in timestamp order. Anything in the directory
 * that is not a well-named `.sql` file throws, so a misnamed migration fails at
 * start instead of being skipped.
 */
export function loadMigrations(dir: URL = MIGRATIONS_DIR): Migration[] {
  return readdirSync(dir)
    .sort()
    .map((name) => {
      const id = name.endsWith('.sql') ? name.slice(0, -'.sql'.length) : name
      if (!name.endsWith('.sql') || !MIGRATION_ID.test(id)) {
        throw new Error(
          `${fileURLToPath(new URL(name, dir))} is not a migration: files there are named ` +
            '<yyyymmdd>T<hhmm>Z_<slug>.sql (UTC, slug in [a-z0-9_])',
        )
      }
      return { id, sql: readFileSync(new URL(name, dir), 'utf8') }
    })
}

export const MIGRATIONS: readonly Migration[] = loadMigrations()

/**
 * Before #491 the ledger recorded migrations by POSITION in a list. Entry `n`
 * of that list is `LEGACY_VERSIONS[n - 1]`; each file holds that entry's SQL
 * byte for byte, so the checksums already recorded still match. `migrate`
 * rewrites positional rows to these ids once, and keeps writing `version` (and
 * `story`) for them so an image from before #491 still starts on the ledger.
 * Frozen: nothing is ever added here.
 */
export const LEGACY_VERSIONS: readonly { id: string; story: string }[] = [
  { id: '20260927T2349Z_credentials_settings', story: '#255' },
  { id: '20260928T0107Z_sessions', story: '#300' },
]

/** SHA-256 (hex) of a migration file's SQL, exactly as written; recorded in `ai_migrations.checksum`. */
export function migrationChecksum(migration: Migration): string {
  return createHash('sha256').update(migration.sql, 'utf8').digest('hex')
}

/**
 * An applied file's SQL no longer matches what was recorded: someone edited a
 * merged migration. Permanent, so main.ts stops the process on it rather than
 * retrying.
 */
export class MigrationChecksumError extends Error {
  override name = 'MigrationChecksumError'
}

/**
 * The ledger cannot be read as this build's: it holds a positional row (from
 * before #491) that main's list never had, i.e. this database ran a migration
 * from a branch that did not merge in that form. Permanent, like
 * MigrationChecksumError: guessing which file it was would either re-run or
 * skip real schema.
 */
export class MigrationLedgerError extends Error {
  override name = 'MigrationLedgerError'
}

export type MigrateOptions = {
  /**
   * Postgres `lock_timeout` while migrating. Bounds the wait for the advisory
   * lock (another pod migrating, or one stuck holding it) and for table locks;
   * a timeout fails this attempt, and `ready()` (db.ts) tries again on its
   * next call.
   */
  lockTimeoutMs?: number
  /** Postgres `statement_timeout` for each statement of the migration. */
  statementTimeoutMs?: number
}

export const DEFAULT_LOCK_TIMEOUT_MS = 10_000
export const DEFAULT_STATEMENT_TIMEOUT_MS = 60_000

/**
 * Applies the migration files this database has not seen, in timestamp order;
 * returns their ids.
 *
 * Every file's SHA-256 is recorded, and an applied file whose SQL has since
 * changed throws MigrationChecksumError (the RULES above, enforced). Rows
 * applied before the checksum column existed (#354) have none; they are
 * adopted with the checksum of the SQL as it is now, which was the SQL #354
 * merged since the rules forbid editing it, and are checked from then on.
 *
 * A ledger row naming a file this build does not have (a newer image ran it,
 * or a dev database ran a branch's) is left alone.
 */
export async function migrate(
  sql: Sql,
  migrations: readonly Migration[] = MIGRATIONS,
  options: MigrateOptions = {},
): Promise<string[]> {
  const lockTimeout = Math.max(1, Math.round(options.lockTimeoutMs ?? DEFAULT_LOCK_TIMEOUT_MS))
  const statementTimeout = Math.max(1, Math.round(options.statementTimeoutMs ?? DEFAULT_STATEMENT_TIMEOUT_MS))
  const ordered = [...migrations].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
  return sql.begin(async (tx) => {
    // SET LOCAL: scoped to this transaction, so the pooled connection goes back
    // without them. Values are integers built here, not input.
    await tx.unsafe(`SET LOCAL lock_timeout = ${lockTimeout}`)
    await tx.unsafe(`SET LOCAL statement_timeout = ${statementTimeout}`)
    await tx`SELECT pg_advisory_xact_lock(${MIGRATION_LOCK.toString()}::bigint)`
    await ensureLedger(tx)
    const rows = await tx<{ id: string; checksum: string | null }[]>`SELECT id, checksum FROM ai_migrations`
    const recorded = new Map(rows.map((row) => [row.id, row.checksum]))
    const applied: string[] = []
    for (const migration of ordered) {
      const checksum = migrationChecksum(migration)
      if (recorded.has(migration.id)) {
        const was = recorded.get(migration.id)
        if (was === null || was === undefined) {
          await tx`UPDATE ai_migrations SET checksum = ${checksum} WHERE id = ${migration.id}`
        } else if (was !== checksum) {
          throw new MigrationChecksumError(
            `ai migration ${migration.id} was applied with different SQL ` +
              `(recorded sha256 ${was.slice(0, 12)}…, now ${checksum.slice(0, 12)}…). Merged files in ` +
              'agent/src/db/migrations/ must never be edited: restore it and add a new file instead.',
          )
        }
        continue
      }
      await tx.unsafe(migration.sql)
      const position = LEGACY_VERSIONS.findIndex((legacy) => legacy.id === migration.id)
      const legacy = position === -1 ? null : { version: position + 1, story: LEGACY_VERSIONS[position]!.story }
      await tx`
        INSERT INTO ai_migrations (id, version, story, checksum)
        VALUES (${migration.id}, ${legacy?.version ?? null}, ${legacy?.story ?? null}, ${checksum})`
      applied.push(migration.id)
    }
    return applied
  })
}

/**
 * Creates the ledger, or converts one from before #491 (keyed by position) to
 * file ids, in the caller's transaction and under its advisory lock, so it
 * happens exactly once however many pods start together.
 */
async function ensureLedger(tx: TransactionSql): Promise<void> {
  const columns = new Set(
    (
      await tx<{ column_name: string }[]>`
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'ai_migrations'`
    ).map((row) => row.column_name),
  )
  if (columns.size === 0) {
    // `version` and `story` are kept for the LEGACY_VERSIONS files only.
    await tx`
      CREATE TABLE ai_migrations (
        id         text PRIMARY KEY,
        version    integer UNIQUE,
        story      text,
        checksum   text,
        applied_at timestamptz NOT NULL DEFAULT now()
      )`
    return
  }
  if (columns.has('id')) return

  const rows = await tx<{ version: number }[]>`SELECT version FROM ai_migrations ORDER BY version`
  const unknown = rows.map((row) => row.version).filter((v) => v < 1 || v > LEGACY_VERSIONS.length)
  if (unknown.length > 0) {
    throw new MigrationLedgerError(
      `ai_migrations records positional version(s) ${unknown.join(', ')}, but main only ever had ` +
        `${LEGACY_VERSIONS.length}: this database ran a migration from a branch that has not merged as such. ` +
        'Recreate the database, or delete those rows and the schema they created, then start again.',
    )
  }
  const [pkey] = await tx<{ conname: string }[]>`
    SELECT conname FROM pg_constraint WHERE conrelid = 'ai_migrations'::regclass AND contype = 'p'`
  await tx`ALTER TABLE ai_migrations ADD COLUMN IF NOT EXISTS checksum text`
  await tx`ALTER TABLE ai_migrations ADD COLUMN id text`
  await tx`
    UPDATE ai_migrations
    SET id = (${LEGACY_VERSIONS.map((legacy) => legacy.id)}::text[])[version]`
  if (pkey) await tx.unsafe(`ALTER TABLE ai_migrations DROP CONSTRAINT "${pkey.conname.replaceAll('"', '""')}"`)
  await tx`
    ALTER TABLE ai_migrations
      ALTER COLUMN id SET NOT NULL,
      ADD PRIMARY KEY (id),
      ADD UNIQUE (version),
      ALTER COLUMN version DROP NOT NULL,
      ALTER COLUMN story DROP NOT NULL`
}
