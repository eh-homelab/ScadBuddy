import type { Sql } from 'postgres'
import { crudOf, type CrudTarget } from '../api/crudStores.js'
import expected from './expected-migrations.json' with { type: 'json' }

// The agent no longer migrates (#5299 D6): only the CRUD service may hold SQL access
// (#5295), so the API applies the agent's files at its start
// (backend/scadbuddy/render/agent_migrations.py, ledger `ai_migrations`), and the agent
// waits until the ledger holds every file this build was written against.
//
// Those files are listed in ./expected-migrations.json, by id and SHA-256: the agent
// image does not carry them. test/schema.test.ts fails when the list differs from
// backend/scadbuddy/migrations_agent/, or when the API's runner reads those files
// differently, so a new agent migration is also an entry here
// (`UPDATE_EXPECTED_MIGRATIONS=1 pnpm exec vitest run test/schema.test.ts` writes it).

/** One file the agent's code needs applied: its id in `ai_migrations` and its SQL's SHA-256. */
export type ExpectedMigration = { id: string; sha256: string }

export const EXPECTED_MIGRATIONS: readonly ExpectedMigration[] = expected

/**
 * The ledger records a file this build expects with other SQL than the build was written
 * against: someone edited a merged migration. Permanent, so main.ts stops the process.
 */
export class MigrationChecksumError extends Error {
  override name = 'MigrationChecksumError'
}

/**
 * The API had not applied every expected file when the wait ran out; `ready()` tries again
 * on its next call. The wait has said so already, so `ready()` does not pass it to
 * `onMigrationError`.
 */
export class MigrationsPendingError extends Error {
  override name = 'MigrationsPendingError'
}

export type AwaitMigrationsOptions = {
  /** How many times the ledger is read before the wait gives up (the first read included). */
  attempts?: number
  /** Milliseconds between reads. */
  intervalMs?: number
  /** Where the wait says it is waiting: once per wait, and never when the ledger is complete. */
  log?: (line: string) => void
  /** Told at each read whether files are still missing (`Database.waitingForMigrations`). */
  onWaiting?: (waiting: boolean) => void
}

export const DEFAULT_AWAIT_ATTEMPTS = 30
export const DEFAULT_AWAIT_INTERVAL_MS = 1_000

/**
 * Resolves once `ai_migrations` holds every file of `expected`; reads it at most
 * `attempts` times, `intervalMs` apart, then throws MigrationsPendingError. A row that
 * records another checksum throws MigrationChecksumError at once. A row with no checksum
 * (from before #354's column, adopted by the API's next start) counts as applied.
 */
export async function awaitMigrations(
  read: LedgerReader,
  expected: readonly ExpectedMigration[] = EXPECTED_MIGRATIONS,
  options: AwaitMigrationsOptions = {},
): Promise<void> {
  const attempts = Math.max(1, options.attempts ?? DEFAULT_AWAIT_ATTEMPTS)
  const intervalMs = options.intervalMs ?? DEFAULT_AWAIT_INTERVAL_MS
  const log = options.log ?? ((line: string) => console.log(line))
  const missing: ExpectedMigration[] = []
  let positional: boolean
  for (let attempt = 1; ; attempt++) {
    const ledger = await read(expected)
    positional = ledger === 'positional'
    const recorded = ledger === 'positional' ? new Map<string, string | null>() : ledger
    missing.length = 0
    for (const migration of expected) {
      if (!recorded.has(migration.id)) {
        missing.push(migration)
        continue
      }
      const checksum = recorded.get(migration.id)
      if (checksum != null && checksum !== migration.sha256) {
        throw new MigrationChecksumError(
          `ai migration ${migration.id} was applied with different SQL (recorded sha256 ${checksum.slice(0, 12)}…, ` +
            `this build expects ${migration.sha256.slice(0, 12)}…): a merged file in ` +
            'backend/scadbuddy/migrations_agent/ was edited.',
        )
      }
    }
    options.onWaiting?.(missing.length > 0)
    if (missing.length === 0) return
    if (attempt === 1) {
      log(
        positional
          ? "waiting for the API to apply the agent's database migrations: ai_migrations is still keyed by " +
              "position (before #491); the API's next start rewrites it, or stops if it holds a row main " +
              "never had (see the API's log)"
          : `waiting for the API to apply the agent's database migrations: ${missing.length} of ${expected.length} ` +
              `not in ai_migrations yet (first ${missing[0]!.id})`,
      )
    }
    if (attempt >= attempts) break
    await new Promise((resolve) => setTimeout(resolve, intervalMs))
  }
  throw new MigrationsPendingError(
    `the API has not applied ${missing.length} of the agent's ${expected.length} migrations ` +
      `(first ${missing[0]!.id}) ${attempts === 1 ? 'in' : `after ${attempts} reads of`} ai_migrations` +
      (positional ? ' (still keyed by position, before #491)' : '') +
      '; is the API at this build or newer?',
  )
}

/** Reads the expected files' rows of the ledger (`readLedger`, below): SQL or the API's route. */
export type LedgerReader = (expected: readonly ExpectedMigration[]) => Promise<Map<string, string | null> | 'positional'>

/** `LedgerReader` over SQL (the agent's own connection). */
export const sqlLedger =
  (sql: Sql): LedgerReader =>
  (expected) =>
    readLedger(sql, expected)

/** `LedgerReader` through the API's route (area `migrations`, backend api/crud_agent_start.py). */
export function crudLedger(target: CrudTarget): LedgerReader {
  const call = crudOf(target)
  return async (expected) => {
    const ledger = await call<{ found: boolean; keyed: boolean; rows: { id: string; checksum: string | null }[] }>(
      "the agent's migrations",
      '/migrations/ledger',
      { method: 'POST', body: { ids: expected.map((m) => m.id) } },
    )
    if (!ledger.found) return new Map()
    if (!ledger.keyed) return 'positional'
    return new Map(ledger.rows.map((row) => [row.id, row.checksum]))
  }
}

/**
 * The expected files' rows of the ledger, id to checksum; none while the API has not made
 * the ledger, and `positional` while it is still keyed by position (no `id` column, before
 * #491), which the API's runner rewrites at its next start; a row main never had stops
 * that start instead (AgentMigrationLedgerError).
 */
async function readLedger(
  sql: Sql,
  expected: readonly ExpectedMigration[],
): Promise<Map<string, string | null> | 'positional'> {
  const [ledger] = await sql<{ found: boolean; keyed: boolean }[]>`
    SELECT to_regclass('ai_migrations') IS NOT NULL AS found,
           EXISTS (SELECT 1 FROM pg_attribute
                   WHERE attrelid = to_regclass('ai_migrations') AND attname = 'id' AND NOT attisdropped) AS keyed`
  if (!ledger?.found) return new Map()
  if (!ledger.keyed) return 'positional'
  const ids = expected.map((m) => m.id)
  const rows = await sql<{ id: string; checksum: string | null }[]>`
    SELECT id, checksum FROM ai_migrations WHERE id = ANY(${ids}::text[])`
  return new Map(rows.map((row) => [row.id, row.checksum]))
}
